"""Cascade link-test dispatch (TZ-5 §4.3, TZ-4 §3.4).

`POST /cascades/{id}/test` (api/routes/cascades.py) starts a `RunTest` Command against
the cascade's entry node and must return `202` with a `test_id` immediately - it does
not block on the Agent actually running the test. This module owns turning that into a
persisted `LinkTest` row (created in a pending state, `outcome=None`) plus the
background task that fills in the real outcome once (or if) a `TestResult` correlates
back through the same `SessionRegistry.resolve()` mechanism `CommandResult` already
uses (`grpc_server/servicer.py::dispatch_test`) - or the dispatch times out, or the
node was never connected in the first place.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.grpc_server.servicer import (
    CommandDispatchTimeoutError,
    NodeNotConnectedError,
    dispatch_test,
    new_command_id,
)
from dist_vpn_platform.models.base import utcnow
from dist_vpn_platform.models.link_test import LinkTest, TestOutcome
from dist_vpn_platform.services.event_bus import publish_link_test_resolved

logger = logging.getLogger(__name__)

# TZ-4 §3.4 / TZ-5 §4.3 describe RunTest's *purpose* (probe the link end-to-end
# through a throwaway client config) but do not fix concrete values for
# preset_client_config_json/probe_url/timeout_seconds - chosen here, and documented
# because the independently-written Agent track needs to know what it will actually
# receive on the wire:
#   - preset_client_config_json: empty. The Agent already knows the cascade's active
#     Hop A preset (it applied the AddInbound during cascade assembly, see
#     services/xray_templates.py) - re-deriving and shipping a full duplicate Xray
#     client-config JSON blob down the wire for every single test request is not
#     required for this slice; the Agent is expected to build its own throwaway
#     client config for testing against its own already-configured inbound. A real
#     Platform-generated payload here is a reasonable follow-up if the Agent track
#     needs one instead of deriving it locally.
#   - probe_url: a well-known, cheap, widely-reachable HTTPS endpoint. Not user
#     traffic (TZ-5 §6.4's destination-privacy guarantee is about VPN *users'*
#     traffic, not Platform-initiated infrastructure probing), so no privacy concern
#     picking a fixed one.
#   - timeout_seconds: generous enough for a cold Xray-side outbound connection
#     through two hops plus one HTTP round trip, without leaving a LinkTest row
#     "pending" for too long if the link really is broken.
DEFAULT_PROBE_URL = "https://cloudflare.com/cdn-cgi/trace"
DEFAULT_TIMEOUT_SECONDS = 15

# How long the Platform itself waits for a correlated TestResult before giving up and
# recording a TIMEOUT outcome - deliberately looser than RunTest.timeout_seconds above
# so the Agent's own timeout has a chance to fire first and report a real, specific
# TestOutcome (TZ-5 §4.3 - "not just ok/fail, but which step failed") instead of the
# Platform guessing generically.
_DISPATCH_WAIT_SECONDS = float(DEFAULT_TIMEOUT_SECONDS + 15)

_OUTCOME_FROM_PROTO = {
    pb.TEST_OUTCOME_SUCCESS: TestOutcome.SUCCESS,
    pb.TEST_OUTCOME_CONTAINER_START_FAILED: TestOutcome.CONTAINER_START_FAILED,
    pb.TEST_OUTCOME_TLS_HANDSHAKE_FAILED: TestOutcome.TLS_HANDSHAKE_FAILED,
    pb.TEST_OUTCOME_PROXY_NO_RESPONSE: TestOutcome.PROXY_NO_RESPONSE,
    pb.TEST_OUTCOME_TIMEOUT: TestOutcome.TIMEOUT,
}

# asyncio.create_task() results must be referenced somewhere or the task can be
# garbage-collected mid-execution (a well-known asyncio gotcha - see the "Important"
# note in the stdlib docs for asyncio.create_task) - this module's background
# resolution tasks are fire-and-forget from the caller's point of view, so this set is
# the only thing keeping them alive until they finish.
_background_tasks: set[asyncio.Task] = set()


def _track_background_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def wait_for_background_tasks(timeout: float = 5.0) -> None:
    """Test-only: block until every currently-tracked background resolution task has
    finished (or `timeout` elapses).

    Without this, a test can tear down its per-test SQLite `db_engine` (see
    tests/conftest.py) while a fire-and-forget `_await_and_resolve()` task from an
    earlier `POST /cascades/{id}/test` in the same test is still mid-flight - the
    background task then races `engine.dispose()`/the sqlite file unlink, surfacing as
    a confusing 'Task exception was never retrieved' plus a locked-file teardown error
    instead of a real assertion failure. tests/conftest.py's `db_engine` fixture calls
    this right before disposing the engine, after `app`/`client` are already torn down.
    Not used by the running application itself - a live Platform process has no
    equivalent teardown point to race against."""
    pending = [task for task in _background_tasks if not task.done()]
    if not pending:
        return
    await asyncio.wait(pending, timeout=timeout)


async def start_link_test(cascade_id: str, entry_node_id: str, *, initiated_by: str = "admin") -> LinkTest:
    """Persist a pending LinkTest row and kick off the RunTest dispatch in the
    background. Returns as soon as the row is committed - never waits on the Agent -
    so the caller can build a 202 response right away.

    The SAME uuid is used as LinkTest.id (the REST resource id - what
    GET /tests/{test_id} and the POST response's `test_id` field both mean),
    LinkTest.test_id (the gRPC correlation column), Command.command_id and
    RunTest.test_id: one identifier end-to-end, REST and gRPC, rather than three
    independently-generated uuids that would need their own mapping table.
    """
    correlation_id = new_command_id()

    async with session_scope() as db:
        link_test = LinkTest(
            id=correlation_id,
            cascade_id=cascade_id,
            test_id=correlation_id,
            initiated_by=initiated_by,
        )
        db.add(link_test)
        await db.flush()
        # expire_on_commit=False (db.py) means `link_test`'s already-loaded column
        # values stay readable after this `async with` block closes the session below
        # - no lazy-load/DetachedInstanceError risk, since nothing here touches a
        # relationship or an attribute that wasn't already set above.

    command = pb.Command(
        command_id=correlation_id,
        run_test=pb.RunTest(
            test_id=correlation_id,
            preset_client_config_json="",
            probe_url=DEFAULT_PROBE_URL,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        ),
    )

    try:
        result_task = await dispatch_test(entry_node_id, command, timeout=_DISPATCH_WAIT_SECONDS)
    except NodeNotConnectedError:
        # Reuses TestOutcome.TIMEOUT - contracts/proto/agent_control.proto's
        # TestOutcome enum has no dedicated "node never connected" member, and this
        # slice cannot add one (contracts/ is out of scope for this change). TIMEOUT
        # plus an explicit `detail` string is the closest honest fit: from the
        # admin's point of view, "no result within a reasonable time" is exactly what
        # happened, and `detail` still surfaces the real reason (TZ-5 §4.3 - "which
        # step failed", not just ok/fail).
        logger.warning(
            "link_test_dispatch_node_offline",
            extra={"cascade_id": cascade_id, "node_id": entry_node_id, "test_id": correlation_id},
        )
        await _resolve(
            correlation_id,
            outcome=TestOutcome.TIMEOUT,
            latency_ms=None,
            detail=f"entry node has no live Agent session (node_id={entry_node_id})",
        )
    else:
        task = asyncio.create_task(_await_and_resolve(correlation_id, result_task))
        _track_background_task(task)

    return link_test


async def _await_and_resolve(test_id: str, result_task: "asyncio.Task[pb.TestResult]") -> None:
    try:
        test_result = await result_task
    except CommandDispatchTimeoutError:
        await _resolve(
            test_id, outcome=TestOutcome.TIMEOUT, latency_ms=None, detail="Agent did not return a result in time"
        )
    except NodeNotConnectedError:
        await _resolve(
            test_id, outcome=TestOutcome.TIMEOUT, latency_ms=None, detail="Agent disconnected before replying"
        )
    except Exception:  # noqa: BLE001 - a background task must never crash silently or take the process down
        logger.exception("link_test_background_resolution_failed", extra={"test_id": test_id})
        await _resolve(
            test_id,
            outcome=TestOutcome.TIMEOUT,
            latency_ms=None,
            detail="internal error while waiting for the Agent's result",
        )
    else:
        outcome = _OUTCOME_FROM_PROTO.get(test_result.outcome, TestOutcome.TIMEOUT)
        await _resolve(test_id, outcome=outcome, latency_ms=test_result.latency_ms, detail=test_result.detail)


async def _resolve(test_id: str, *, outcome: TestOutcome, latency_ms: int | None, detail: str) -> None:
    async with session_scope() as db:
        row = (await db.execute(select(LinkTest).where(LinkTest.id == test_id))).scalar_one_or_none()
        if row is None:
            logger.warning("link_test_resolve_missing_row", extra={"test_id": test_id})
            return
        row.outcome = outcome
        row.latency_ms = latency_ms
        row.detail = detail
        row.completed_at = utcnow()
        # expire_on_commit=False (db.py) - `row`'s attributes stay readable below,
        # after this `async with` block commits and closes the session.

    publish_link_test_resolved(row)  # TZ-5 §4.3: push to WS /events subscribers
    logger.info("link_test_resolved", extra={"test_id": test_id, "outcome": outcome.value})
