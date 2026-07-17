"""AgentControl gRPC servicer (contracts/proto/agent_control.proto).

Scope for this slice (see platform/README.md "Что это делает" / TODO):
  - Enrollment: validated against a seeded/real EnrollmentToken row, creates the Node
    (status ENROLLING), returns EnrollmentAck. mTLS client certificate issuance
    (TZ-5 §6.2) is real: the submitted CSR is parsed, self-signature-verified, and
    signed by the Platform's own CA (security/mtls_ca.py) - `mtls_client_cert_pem` is
    the resulting cert PEM. Enrollment is rejected if csr_pem is missing/malformed.
  - Heartbeat: updates Node.last_heartbeat_at + health fields, and performs the
    ENROLLING -> HEALTHY / DOWN|DEGRADED -> HEALTHY transitions (TZ-5 §4.1), logging a
    HealthEvent on any status change.
  - TelemetryReport: persisted into UsageStat rows (TZ-5 §4.4/§6.4 - byte counters only).
  - Command dispatch (Platform -> Agent): a `SessionRegistry` tracks each connected
    Agent's open bidi-stream (keyed by node_id) so `dispatch_command()` can enqueue a
    real `Command` onto that node's stream and await the matching `CommandResult`,
    correlated by `command_id`. This used to be a documented stub
    (`raise NotImplementedError`, see git history on this file) - now implemented per
    the command-dispatch slice.
  - `dispatch_test()`: the same enqueue+correlate mechanism as `dispatch_command()`,
    but non-blocking (returns an `asyncio.Task` instead of awaiting inline) and
    correlated by `TestResult.test_id` through the same `session.pending` dict -
    POST /cascades/{id}/test (services/link_tests.py) uses this so the HTTP response
    doesn't block on the Agent actually running the test (TZ-5 §4.3).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import grpc
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.generated import agent_control_pb2_grpc as pb_grpc
from dist_vpn_platform.models.base import as_aware_utc
from dist_vpn_platform.models.cascade import Cascade, CascadeStatus
from dist_vpn_platform.models.enrollment_token import EnrollmentToken
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.models.node_resource_sample import NodeResourceSample
from dist_vpn_platform.models.usage_stat import UsageStat
from dist_vpn_platform.models.user import User, UserCascade
from dist_vpn_platform.models.user_device_kick_event import UserDeviceKickEvent
from dist_vpn_platform.schemas.cascades import CascadeOut
from dist_vpn_platform.security import mtls_ca
from dist_vpn_platform.security.enrollment_tokens import hash_token
from dist_vpn_platform.services import xray_templates as xray
from dist_vpn_platform.services.event_bus import publish_cascade_event, publish_health_event

logger = logging.getLogger(__name__)

_ROLE_FROM_PROTO = {
    pb.NODE_ROLE_ENTRY: NodeRole.ENTRY,
    pb.NODE_ROLE_EXIT: NodeRole.EXIT,
}


# --------------------------------------------------------------------------------
# Live per-node command dispatch (Platform -> Agent).
#
# Each `Session()` bidi-stream gets its own asyncio.Queue ("outbox") that the writer
# side of the generator drains and yields onto the wire. `SessionRegistry` maps
# node_id -> that queue (+ a dict of in-flight command/test futures) so code outside
# the gRPC layer entirely (REST route handlers) can call `dispatch_command()` and
# `await` a real reply, without either side needing to know about gRPC internals.
# --------------------------------------------------------------------------------


class NodeNotConnectedError(RuntimeError):
    """Raised by dispatch_command() when node_id has no open AgentControl session
    right now (Agent never enrolled/heartbeated yet, or has since disconnected)."""


class CommandDispatchTimeoutError(RuntimeError):
    """Raised by dispatch_command() when the Agent did not send back a matching
    CommandResult within the timeout."""


def new_command_id() -> str:
    return str(uuid.uuid4())


@dataclass
class NodeSession:
    node_id: str
    outbox: "asyncio.Queue[pb.PlatformMessage]"
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class SessionRegistry:
    """In-memory registry of currently-connected Agents' open bidi-streams, keyed by
    node_id. This is the "реестр открытых bidi-стримов по node_id" the previous
    slice's TODO described. Single-process only (fine for now - one Platform process
    owns the gRPC server, see main.py); fanning dispatch_command out across multiple
    Platform replicas would need a shared broker instead - out of scope here.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, NodeSession] = {}

    def bind(self, node_id: str, outbox: "asyncio.Queue[pb.PlatformMessage]") -> NodeSession:
        session = NodeSession(node_id=node_id, outbox=outbox)
        # A reconnect (new stream for a node_id we already know) replaces the old
        # entry outright - the old stream is presumably dead or about to be torn
        # down; see unbind() below for why that's safe even if it races.
        self._sessions[node_id] = session
        return session

    def unbind(self, node_id: str, outbox: "asyncio.Queue[pb.PlatformMessage]") -> None:
        current = self._sessions.get(node_id)
        if current is None or current.outbox is not outbox:
            return  # already replaced by a newer session for this node - leave it alone
        for pending_future in current.pending.values():
            if not pending_future.done():
                pending_future.set_exception(
                    NodeNotConnectedError(f"node {node_id!r} disconnected before replying")
                )
        del self._sessions[node_id]

    def get(self, node_id: str) -> NodeSession | None:
        return self._sessions.get(node_id)

    def resolve(self, node_id: str | None, correlation_id: str, result: object) -> bool:
        """Resolve a pending dispatch_command()/dispatch_test() future for `node_id`
        keyed by `correlation_id` (a CommandResult.command_id or TestResult.test_id).
        Returns False if there was nothing waiting on it (unsolicited/late reply)."""
        if node_id is None:
            return False
        session = self._sessions.get(node_id)
        if session is None:
            return False
        pending_future = session.pending.pop(correlation_id, None)
        if pending_future is None or pending_future.done():
            return False
        pending_future.set_result(result)
        return True


_registry = SessionRegistry()


async def _enqueue_and_track(node_id: str, command: pb.Command) -> tuple[NodeSession, asyncio.Future]:
    """Shared first half of dispatch_command()/dispatch_test(): look up the node's
    live session (raising NodeNotConnectedError immediately if there isn't one),
    register a pending correlation future keyed by `command.command_id`, and enqueue
    the Command onto the node's outbox. Both CommandResult.command_id and
    TestResult.test_id resolve through this same `session.pending` dict (see
    SessionRegistry.resolve() above) - callers that dispatch a RunTest MUST set
    `command.command_id == command.run_test.test_id` for the correlation to find this
    entry when the Agent's reply comes back as a TestResult rather than a
    CommandResult (see services/link_tests.py).
    """
    session = _registry.get(node_id)
    if session is None:
        raise NodeNotConnectedError(f"node {node_id!r} has no open AgentControl session")

    loop = asyncio.get_running_loop()
    result_future: asyncio.Future = loop.create_future()
    session.pending[command.command_id] = result_future
    await session.outbox.put(pb.PlatformMessage(command=command))
    return session, result_future


async def dispatch_command(node_id: str, command: pb.Command, *, timeout: float = 10.0) -> pb.CommandResult:
    """Enqueue `command` onto node_id's live bidi-stream and await the matching
    CommandResult (correlated by `command.command_id`).

    Raises NodeNotConnectedError immediately if the node has no open session right
    now, or CommandDispatchTimeoutError if the Agent doesn't reply within `timeout`
    seconds (e.g. it's connected but wedged).
    """
    session, result_future = await _enqueue_and_track(node_id, command)
    try:
        return await asyncio.wait_for(result_future, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise CommandDispatchTimeoutError(
            f"node {node_id!r} did not return a CommandResult for command "
            f"{command.command_id!r} within {timeout}s"
        ) from exc
    finally:
        session.pending.pop(command.command_id, None)


async def dispatch_test(node_id: str, command: pb.Command, *, timeout: float = 30.0) -> "asyncio.Task[pb.TestResult]":
    """Like dispatch_command(), but for `Command(run_test=...)` specifically and
    non-blocking: enqueues the Command and registers the pending correlation exactly
    like dispatch_command() does (raising NodeNotConnectedError immediately, before
    returning, if the node isn't connected right now), but then returns a Task instead
    of awaiting it inline.

    POST /cascades/{id}/test (TZ-5 §4.3) must return 202 without blocking on the
    Agent's reply - callers await the returned Task in the background instead (see
    services/link_tests.py), which resolves to the correlated TestResult or raises
    CommandDispatchTimeoutError if the Agent doesn't reply within `timeout` seconds.
    """
    session, result_future = await _enqueue_and_track(node_id, command)

    async def _await_result() -> pb.TestResult:
        try:
            return await asyncio.wait_for(result_future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CommandDispatchTimeoutError(
                f"node {node_id!r} did not return a TestResult for test "
                f"{command.command_id!r} within {timeout}s"
            ) from exc
        finally:
            session.pending.pop(command.command_id, None)

    return asyncio.create_task(_await_result())


class AgentControlServicer(pb_grpc.AgentControlServicer):
    async def Session(self, request_iterator, context: grpc.aio.ServicerContext):
        peer = context.peer()
        # server.py binds with require_client_auth=False (see its module docstring for
        # why) - a peer with no client cert can still open this stream at all, so this
        # is the one place that must draw the line the transport layer no longer draws.
        #
        # Found while wiring this up: grpc's C-core does NOT verify an *offered*
        # client cert's chain of trust when require_client_auth=False - that mode is
        # "request but don't verify", not "verify if offered" (require_client_auth=True
        # is the only mode BoringSSL itself chain-checks a client cert in). So
        # `peer_identities()`/`auth_context()` being non-empty only means *a* cert was
        # offered, not that it was ours - trusting that alone would let literally any
        # self-signed cert from anywhere impersonate an enrolled node. Pull the actual
        # PEM bytes grpc exposes for the peer (`x509_pem_cert`, populated whenever any
        # cert was offered, verified or not) and check it ourselves against our own CA.
        cert_pem_values = context.auth_context().get("x509_pem_cert")
        has_client_cert = bool(cert_pem_values) and mtls_ca.verify_client_cert_pem(
            mtls_ca.get_mtls_material().ca, cert_pem_values[0]
        )
        logger.info("grpc_session_opened", extra={"peer": peer, "has_client_cert": has_client_cert})

        # Every PlatformMessage bound for this Agent (enrollment ack *and* dispatched
        # Commands) flows through this single queue, so there is exactly one place
        # that yields onto the wire - see the writer loop below.
        outbox: "asyncio.Queue[pb.PlatformMessage | None]" = asyncio.Queue()
        state: dict[str, str | None] = {"node_id": None}

        async def _pump_inbound() -> None:
            try:
                async for message in request_iterator:
                    kind = message.WhichOneof("payload")
                    if not has_client_cert and kind != "enrollment":
                        # Uncertified peer trying to do anything other than enroll -
                        # this is either a bug in a legitimate Agent or someone probing
                        # the port with no credentials at all. Refuse and close rather
                        # than silently ignoring, so it fails loudly instead of hanging.
                        logger.warning(
                            "grpc_uncertified_peer_rejected", extra={"peer": peer, "kind": kind}
                        )
                        break
                    if kind == "enrollment":
                        ack = await self._handle_enrollment(message.enrollment, peer=peer)
                        if ack.accepted:
                            state["node_id"] = ack.node_id
                            _registry.bind(ack.node_id, outbox)
                            logger.info(
                                "grpc_node_session_bound",
                                extra={"node_id": ack.node_id, "peer": peer, "via": "enrollment"},
                            )
                        await outbox.put(pb.PlatformMessage(enrollment_ack=ack))
                    elif kind == "heartbeat":
                        await self._handle_heartbeat(message.heartbeat)
                        # Reconnect case (TZ-4 §5.2: after first enrollment, an Agent
                        # reconnects using its permanent identity rather than sending
                        # another Enrollment) - bind the session as soon as we learn
                        # the node_id from the first Heartbeat on this stream, if we
                        # haven't already bound it via Enrollment above.
                        if state["node_id"] is None and message.heartbeat.node_id:
                            state["node_id"] = message.heartbeat.node_id
                            _registry.bind(message.heartbeat.node_id, outbox)
                            logger.info(
                                "grpc_node_session_bound",
                                extra={"node_id": message.heartbeat.node_id, "peer": peer, "via": "heartbeat"},
                            )
                    elif kind == "telemetry":
                        await self._handle_telemetry(message.telemetry)
                    elif kind == "command_result":
                        resolved = _registry.resolve(
                            state["node_id"], message.command_result.command_id, message.command_result
                        )
                        logger.info(
                            "command_result_received",
                            extra={
                                "command_id": message.command_result.command_id,
                                "success": message.command_result.success,
                                "resolved": resolved,
                            },
                        )
                    elif kind == "test_result":
                        resolved = _registry.resolve(
                            state["node_id"], message.test_result.test_id, message.test_result
                        )
                        logger.info(
                            "test_result_received",
                            extra={
                                "test_id": message.test_result.test_id,
                                "outcome": message.test_result.outcome,
                                "resolved": resolved,
                            },
                        )
                    else:
                        logger.warning("grpc_unknown_payload", extra={"peer": peer})
            finally:
                await outbox.put(None)  # sentinel: tell the writer loop below to stop

        pump_task = asyncio.create_task(_pump_inbound())
        try:
            while True:
                message = await outbox.get()
                if message is None:
                    break
                yield message
        finally:
            if state["node_id"] is not None:
                _registry.unbind(state["node_id"], outbox)
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
            logger.info("grpc_session_closed", extra={"peer": peer, "node_id": state["node_id"]})

    async def _handle_enrollment(self, enrollment: pb.Enrollment, *, peer: str) -> pb.EnrollmentAck:
        token_hash = hash_token(enrollment.enrollment_token)

        async with session_scope() as db:
            result = await db.execute(
                select(EnrollmentToken).where(EnrollmentToken.token_hash == token_hash)
            )
            token_row = result.scalar_one_or_none()

            if token_row is None:
                logger.warning("enrollment_rejected", extra={"peer": peer, "reason": "unknown_token"})
                return pb.EnrollmentAck(accepted=False, reason="unknown or invalid enrollment token")

            if token_row.used_at is not None:
                logger.warning("enrollment_rejected", extra={"peer": peer, "reason": "token_already_used"})
                return pb.EnrollmentAck(accepted=False, reason="enrollment token already used")

            if as_aware_utc(token_row.expires_at) < datetime.now(timezone.utc):
                logger.warning("enrollment_rejected", extra={"peer": peer, "reason": "token_expired"})
                return pb.EnrollmentAck(accepted=False, reason="enrollment token expired")

            role = _ROLE_FROM_PROTO.get(enrollment.requested_role)
            if role is None:
                return pb.EnrollmentAck(accepted=False, reason="requested_role must be ENTRY or EXIT")

            # TZ-5 §6.2: sign the Agent's PKCS#10 CSR into a short-lived mTLS client
            # cert before touching the DB - a malformed/invalid CSR must not burn the
            # (single-use) enrollment token or create a Node row, same as any other
            # rejection path above. See security/mtls_ca.py for validity/algorithm.
            try:
                ca = mtls_ca.get_mtls_material().ca
                client_cert_pem = mtls_ca.sign_client_csr(ca, enrollment.csr_pem)
            except mtls_ca.CSRValidationError as exc:
                logger.warning(
                    "enrollment_rejected", extra={"peer": peer, "reason": "invalid_csr", "detail": str(exc)}
                )
                return pb.EnrollmentAck(accepted=False, reason=f"invalid csr_pem: {exc}")

            node = Node(
                label=token_row.label,
                hostname=enrollment.hostname,
                role=role,
                agent_version=enrollment.agent_version,
                status=NodeStatus.ENROLLING,
                enrollment_token_id=token_row.id,
                mtls_cert_fingerprint=mtls_ca.fingerprint_sha256_hex(client_cert_pem),
            )
            db.add(node)
            token_row.used_at = datetime.now(timezone.utc)
            await db.flush()

            logger.info(
                "enrollment_accepted",
                extra={"peer": peer, "node_id": node.id, "role": role.value},
            )
            return pb.EnrollmentAck(node_id=node.id, accepted=True, mtls_client_cert_pem=client_cert_pem)

    async def _handle_heartbeat(self, heartbeat: pb.Heartbeat) -> None:
        # `event`/`cascade_events`/`recovered_cascades` are published only after the
        # transaction below actually commits (see the `async with` block's exit) -
        # same publish-after-commit ordering as api/routes/cascades.py::create_cascade
        # and api/routes/nodes.py::decommission_node, so a WS /events subscriber is
        # never notified about a status change that a concurrent rollback could still
        # discard.
        event: HealthEvent | None = None
        cascade_events: list[HealthEvent] = []
        recovered_cascades: list[Cascade] = []

        async with session_scope() as db:
            node = await db.get(Node, heartbeat.node_id)
            if node is None:
                logger.warning("heartbeat_unknown_node", extra={"node_id": heartbeat.node_id})
                return

            previous_status = node.status
            node.last_heartbeat_at = datetime.fromtimestamp(heartbeat.timestamp_unix, tz=timezone.utc)
            node.xray_container_running = heartbeat.health.xray_container_running
            node.cpu_percent = heartbeat.health.cpu_percent
            node.mem_percent = heartbeat.health.mem_percent
            node.disk_percent = heartbeat.health.disk_percent

            # TZ-5 §4.1: ENROLLING -> HEALTHY after the first successful heartbeat.
            # Recovery from DOWN/DEGRADED -> HEALTHY. Automatic DEGRADED-from-resource-
            # thresholds and DOWN-from-missed-heartbeat-timeout are still TODO for a
            # follow-up slice (needs a background scheduler, see platform/README.md).
            if heartbeat.health.xray_container_running and previous_status in (
                NodeStatus.ENROLLING,
                NodeStatus.DOWN,
                NodeStatus.DEGRADED,
            ):
                node.status = NodeStatus.HEALTHY

            # NodeResourceSample: one row per heartbeat, historizing what the
            # cpu_percent/mem_percent/disk_percent columns above only ever hold as a
            # single latest-value snapshot - see that model's own docstring, including
            # the explicit "no retention/pruning yet, rows grow unbounded" caveat.
            # online_user_count/online_device_count come from _handle_user_online's
            # cache on this same Node row (may be stale if TelemetryReport hasn't
            # arrived yet this cycle, or ever, for a brand-new node - not a bug, see
            # that method's docstring).
            provisioned_user_count = (
                await db.execute(
                    select(func.count(func.distinct(UserCascade.user_id)))
                    .select_from(UserCascade)
                    .join(Cascade, Cascade.id == UserCascade.cascade_id)
                    .where(Cascade.entry_node_id == node.id)
                )
            ).scalar_one()
            db.add(
                NodeResourceSample(
                    node_id=node.id,
                    sampled_at=node.last_heartbeat_at,
                    cpu_percent=node.cpu_percent,
                    mem_percent=node.mem_percent,
                    disk_percent=node.disk_percent,
                    provisioned_user_count=provisioned_user_count,
                    online_user_count=node.last_online_user_count,
                    online_device_count=node.last_online_device_count,
                )
            )

            await db.flush()

            if node.status != previous_status:
                event = HealthEvent(
                    node_id=node.id,
                    from_status=previous_status.value,
                    to_status=node.status.value,
                    reason="heartbeat",
                )
                db.add(event)
                await db.flush()
                logger.info(
                    "node_status_changed",
                    extra={"node_id": node.id, "from": previous_status.value, "to": node.status.value},
                )

                # Cascade recovery: a real, previously-missing counterpart to
                # services/health_scheduler.py's DOWN-marking - that module's own
                # docstring says "recovery is handled reactively by _handle_heartbeat",
                # which was true for the *node*, but nothing here ever actually
                # touched a DOWN *cascade*'s status before this fix - it would have
                # stayed DOWN forever even after its entry node came back HEALTHY.
                # Only entry nodes: exit-node health doesn't drive Cascade.status at
                # all (Hop B failover is Xray's own balancer's job, not Platform's -
                # same reasoning as health_scheduler.py's DOWN-marking side).
                if node.status == NodeStatus.HEALTHY and node.role == NodeRole.ENTRY:
                    down_cascades = (
                        await db.execute(
                            select(Cascade)
                            .options(selectinload(Cascade.exit_links))
                            .where(Cascade.entry_node_id == node.id, Cascade.status == CascadeStatus.DOWN)
                        )
                    ).scalars().all()
                    for cascade in down_cascades:
                        cascade.status = CascadeStatus.UP
                        cascade_events.append(
                            HealthEvent(
                                cascade_id=cascade.id,
                                from_status=CascadeStatus.DOWN.value,
                                to_status=CascadeStatus.UP.value,
                                reason=f"entry node {node.id} heartbeat resumed",
                            )
                        )
                        recovered_cascades.append(cascade)
                    for cascade_event in cascade_events:
                        db.add(cascade_event)
                    await db.flush()

        if event is not None:
            publish_health_event(event)  # TZ-5 §4.3: push to WS /events subscribers
        for cascade_event in cascade_events:
            publish_health_event(cascade_event)
        for cascade in recovered_cascades:
            publish_cascade_event(
                CascadeOut(
                    id=cascade.id,
                    entry_node_id=cascade.entry_node_id,
                    exit_node_ids=cascade.exit_node_ids,
                    status=cascade.status,
                    hop_a_presets=cascade.hop_a_presets,
                    hopa_reality_public_key=cascade.hopa_reality_public_key,
                    hopa_reality_short_id=cascade.hopa_reality_short_id,
                )
            )

    async def _handle_telemetry(self, telemetry: pb.TelemetryReport) -> None:
        """Persist TelemetryReport.counters into UsageStat rows (TZ-5 §4.4/§6.4 - byte
        counters only, never destination/content - that guarantee lives entirely in
        what Agents choose to put on the wire, this method just stores what arrives).

        Counters are bucketed into hourly `period_start` buckets and accumulated
        (upsert-by-add) rather than inserting one row per report, so a chatty Agent
        doesn't produce an unbounded number of rows for the same scope/key/node/hour.
        """
        period_start = datetime.fromtimestamp(telemetry.timestamp_unix, tz=timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )

        async with session_scope() as db:
            for counter in telemetry.counters:
                user_id: str | None = None
                if counter.scope == "user":
                    # AddUser dispatches the same `email` = User.uuid to the Agent
                    # (see api/routes/users.py) - that's the join key back to a
                    # known Platform User for scope="user" counters.
                    resolved = await db.execute(select(User.id).where(User.uuid == counter.key))
                    user_id = resolved.scalar_one_or_none()

                existing = await db.execute(
                    select(UsageStat).where(
                        UsageStat.node_id == telemetry.node_id,
                        UsageStat.scope == counter.scope,
                        UsageStat.raw_key == counter.key,
                        UsageStat.period_start == period_start,
                    )
                )
                row = existing.scalar_one_or_none()
                if row is None:
                    db.add(
                        UsageStat(
                            scope=counter.scope,
                            raw_key=counter.key,
                            user_id=user_id,
                            node_id=telemetry.node_id,
                            period_start=period_start,
                            uplink_bytes=counter.uplink_bytes,
                            downlink_bytes=counter.downlink_bytes,
                        )
                    )
                else:
                    row.uplink_bytes += counter.uplink_bytes
                    row.downlink_bytes += counter.downlink_bytes
                    if user_id is not None:
                        row.user_id = user_id

        await self._handle_user_online(telemetry)

        logger.info(
            "telemetry_ingested",
            extra={"node_id": telemetry.node_id, "counters": len(telemetry.counters)},
        )

    async def _handle_user_online(self, telemetry: pb.TelemetryReport) -> None:
        """Processes TelemetryReport.user_online (Agent's per-node view of currently-
        connected device IPs, via StatsService - see agent/src/dist_vpn_agent/
        xray_client.py::get_users_stats): caches a live-ish device count on each
        known User, and kicks (RemoveUser+AddUser, forcing every device to
        reconnect) any user whose reported online-IP count on THIS node exceeds
        their User.max_devices.

        --- Scope boundary: per-node, not a cross-node aggregate limit ---

        Each TelemetryReport only reflects what ONE node's Xray-core sees - a user
        granted access to multiple cascades on different entry nodes could stay
        under max_devices on every individual node while exceeding it in aggregate
        across all of them combined. Enforcing a true global limit would need
        aggregating recent reports across every node a user has access to (a shared
        cache or cross-report query this slice doesn't build) - this is a known,
        documented limitation, not an overlooked edge case: TZ-5's device-limit
        requirement doesn't specify per-node vs. global, and per-node is what's
        directly measurable from what Xray-core itself reports, without inventing
        cross-node state Platform doesn't otherwise need.
        """
        kicks: list[tuple[User, int]] = []
        node_online_users = 0
        node_online_devices = 0

        async with session_scope() as db:
            for online_stat in telemetry.user_online:
                resolved = await db.execute(select(User).where(User.uuid == online_stat.email))
                user = resolved.scalar_one_or_none()
                if user is None:
                    continue

                online_count = len(online_stat.online_ips)
                user.last_online_device_count = online_count
                user.last_online_at = datetime.fromtimestamp(telemetry.timestamp_unix, tz=timezone.utc)
                if online_count:
                    node_online_users += 1
                    node_online_devices += online_count

                if user.max_devices is not None and online_count > user.max_devices:
                    db.add(
                        UserDeviceKickEvent(
                            user_id=user.id,
                            node_id=telemetry.node_id,
                            online_ip_count=online_count,
                            max_devices=user.max_devices,
                        )
                    )
                    kicks.append((user, online_count))

            # Cached for _handle_heartbeat's NodeResourceSample writes - see Node's
            # own docstring for why this can't just be computed at heartbeat-time
            # from User rows directly (a User's online count isn't node-scoped).
            node = await db.get(Node, telemetry.node_id)
            if node is not None:
                node.last_online_user_count = node_online_users
                node.last_online_device_count = node_online_devices

            await db.flush()
            # Capture what dispatch needs before the session (and its identity map)
            # closes at this `async with` block's exit - id()/uuid values, not ORM
            # objects, so nothing below touches a detached instance.
            kick_ids = [(u.id, u.uuid, count) for u, count in kicks]

        for user_id, user_uuid, online_count in kick_ids:
            await self._kick_user_devices(user_id, user_uuid, telemetry.node_id)
            logger.warning(
                "user_device_limit_kicked",
                extra={
                    "user_id": user_id,
                    "node_id": telemetry.node_id,
                    "online_ip_count": online_count,
                },
            )

    async def _kick_user_devices(self, user_id: str, user_uuid: str, node_id: str) -> None:
        """Forces every current device of `user_uuid` to reconnect, on every cascade
        this user has access to whose entry node is `node_id` - RemoveUser then
        AddUser back-to-back on the same inbound_tag(s), reusing the exact commands
        api/routes/users.py already dispatches for revoke/grant, just triggered here
        instead of by an admin action. Best-effort per cascade (mirrors
        api/command_dispatch.py's dispatch_best_effort, not reused directly to avoid
        importing api/ from grpc_server/ - see that module's own docstring for the
        layering reason)."""
        async with session_scope() as db:
            stmt = (
                select(Cascade)
                .join(UserCascade, UserCascade.cascade_id == Cascade.id)
                .where(UserCascade.user_id == user_id, Cascade.entry_node_id == node_id)
            )
            cascades = (await db.execute(stmt)).scalars().all()
            cascade_ids = [c.id for c in cascades]

        for cascade_id in cascade_ids:
            inbound_tag = xray.hopa_inbound_tag(cascade_id)
            for command in (
                pb.Command(
                    command_id=new_command_id(),
                    remove_user=pb.RemoveUser(inbound_tag=inbound_tag, email=user_uuid),
                ),
                pb.Command(
                    command_id=new_command_id(),
                    add_user=pb.AddUser(inbound_tag=inbound_tag, uuid=user_uuid, email=user_uuid, flow=""),
                ),
            ):
                try:
                    await dispatch_command(node_id, command)
                except (NodeNotConnectedError, CommandDispatchTimeoutError) as err:
                    logger.warning(
                        "user_device_kick_dispatch_failed",
                        extra={"user_id": user_id, "node_id": node_id, "cascade_id": cascade_id, "error": str(err)},
                    )
                    break  # don't AddUser back if RemoveUser itself never landed
