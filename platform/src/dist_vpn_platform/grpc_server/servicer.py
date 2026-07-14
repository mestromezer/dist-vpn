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
    correlated by `command_id` (same mechanism for `TestResult`/`test_id`). This used
    to be a documented stub (`raise NotImplementedError`, see git history on this
    file) - now implemented per the command-dispatch slice.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import grpc
from sqlalchemy import select

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.generated import agent_control_pb2_grpc as pb_grpc
from dist_vpn_platform.models.base import as_aware_utc
from dist_vpn_platform.models.enrollment_token import EnrollmentToken
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.models.usage_stat import UsageStat
from dist_vpn_platform.models.user import User
from dist_vpn_platform.security import mtls_ca
from dist_vpn_platform.security.enrollment_tokens import hash_token
from dist_vpn_platform.services.event_bus import publish_health_event

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


async def dispatch_command(node_id: str, command: pb.Command, *, timeout: float = 10.0) -> pb.CommandResult:
    """Enqueue `command` onto node_id's live bidi-stream and await the matching
    CommandResult (correlated by `command.command_id`).

    Raises NodeNotConnectedError immediately if the node has no open session right
    now, or CommandDispatchTimeoutError if the Agent doesn't reply within `timeout`
    seconds (e.g. it's connected but wedged).
    """
    session = _registry.get(node_id)
    if session is None:
        raise NodeNotConnectedError(f"node {node_id!r} has no open AgentControl session")

    loop = asyncio.get_running_loop()
    result_future: asyncio.Future = loop.create_future()
    session.pending[command.command_id] = result_future
    await session.outbox.put(pb.PlatformMessage(command=command))
    try:
        return await asyncio.wait_for(result_future, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise CommandDispatchTimeoutError(
            f"node {node_id!r} did not return a CommandResult for command "
            f"{command.command_id!r} within {timeout}s"
        ) from exc
    finally:
        session.pending.pop(command.command_id, None)


class AgentControlServicer(pb_grpc.AgentControlServicer):
    async def Session(self, request_iterator, context: grpc.aio.ServicerContext):
        peer = context.peer()
        logger.info("grpc_session_opened", extra={"peer": peer})

        # Every PlatformMessage bound for this Agent (enrollment ack *and* dispatched
        # Commands) flows through this single queue, so there is exactly one place
        # that yields onto the wire - see the writer loop below.
        outbox: "asyncio.Queue[pb.PlatformMessage | None]" = asyncio.Queue()
        state: dict[str, str | None] = {"node_id": None}

        async def _pump_inbound() -> None:
            try:
                async for message in request_iterator:
                    kind = message.WhichOneof("payload")
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
        # `event` is published only after the transaction below actually commits (see
        # the `async with` block's exit) - same publish-after-commit ordering as
        # api/routes/cascades.py::create_cascade and api/routes/nodes.py::decommission_
        # node, so a WS /events subscriber is never notified about a status change that
        # a concurrent rollback could still discard.
        event: HealthEvent | None = None

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

        if event is not None:
            publish_health_event(event)  # TZ-5 §4.3: push to WS /events subscribers

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

        logger.info(
            "telemetry_ingested",
            extra={"node_id": telemetry.node_id, "counters": len(telemetry.counters)},
        )
