"""AgentControl gRPC servicer (contracts/proto/agent_control.proto).

Scope for this slice (see platform/README.md "Scope of this slice / TODO"):
  - Enrollment: validated against a seeded/real EnrollmentToken row, creates the Node
    (status ENROLLING), returns EnrollmentAck. mTLS certificate issuance (TZ-5 §6.2,
    step-ca) is a documented TODO - `mtls_client_cert_pem` is empty for now.
  - Heartbeat: updates Node.last_heartbeat_at + health fields, and performs the
    ENROLLING -> HEALTHY / DOWN|DEGRADED -> HEALTHY transitions (TZ-5 §4.1), logging a
    HealthEvent on any status change.
  - TelemetryReport / CommandResult / TestResult: accepted and logged, but NOT yet
    persisted into UsageStat / RotationRecord / LinkTest - explicit TODOs below, this
    is the "command-dispatch" work deferred to a follow-up slice per the task scope.
  - Command dispatch (Platform -> Agent) is entirely a documented stub - see
    `dispatch_command` below - no cascade-assembly logic exists yet to originate them.
"""
from __future__ import annotations

import logging
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
from dist_vpn_platform.security.enrollment_tokens import hash_token

logger = logging.getLogger(__name__)

_ROLE_FROM_PROTO = {
    pb.NODE_ROLE_ENTRY: NodeRole.ENTRY,
    pb.NODE_ROLE_EXIT: NodeRole.EXIT,
}


class AgentControlServicer(pb_grpc.AgentControlServicer):
    async def Session(self, request_iterator, context: grpc.aio.ServicerContext):
        peer = context.peer()
        logger.info("grpc_session_opened", extra={"peer": peer})
        try:
            async for message in request_iterator:
                kind = message.WhichOneof("payload")
                if kind == "enrollment":
                    ack = await self._handle_enrollment(message.enrollment, peer=peer)
                    yield pb.PlatformMessage(enrollment_ack=ack)
                elif kind == "heartbeat":
                    await self._handle_heartbeat(message.heartbeat)
                    # No direct ack in the protocol for Heartbeat; a dedicated keep-alive
                    # Ping loop (independent of inbound messages) is TODO for this slice.
                elif kind == "telemetry":
                    self._handle_telemetry_stub(message.telemetry)
                elif kind == "command_result":
                    self._handle_command_result_stub(message.command_result)
                elif kind == "test_result":
                    self._handle_test_result_stub(message.test_result)
                else:
                    logger.warning("grpc_unknown_payload", extra={"peer": peer})
        finally:
            logger.info("grpc_session_closed", extra={"peer": peer})

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

            node = Node(
                label=token_row.label,
                hostname=enrollment.hostname,
                role=role,
                agent_version=enrollment.agent_version,
                status=NodeStatus.ENROLLING,
                enrollment_token_id=token_row.id,
            )
            db.add(node)
            token_row.used_at = datetime.now(timezone.utc)
            await db.flush()

            logger.info(
                "enrollment_accepted",
                extra={"peer": peer, "node_id": node.id, "role": role.value},
            )
            # TODO(TZ-5 §6.2): issue a real short-lived mTLS client certificate via
            # step-ca instead of returning an empty placeholder.
            return pb.EnrollmentAck(node_id=node.id, accepted=True, mtls_client_cert_pem=b"")

    async def _handle_heartbeat(self, heartbeat: pb.Heartbeat) -> None:
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
            # Recovery from DOWN/DEGRADED -> HEALTHY. Anything more nuanced (marking
            # DEGRADED from resource thresholds, DOWN from missed-heartbeat timeout) is
            # TODO for the cascade-assembly/failover follow-up slice (TZ-5 §4.2) - this
            # slice only proves the wiring end-to-end per the acceptance criteria.
            if heartbeat.health.xray_container_running and previous_status in (
                NodeStatus.ENROLLING,
                NodeStatus.DOWN,
                NodeStatus.DEGRADED,
            ):
                node.status = NodeStatus.HEALTHY

            await db.flush()

            if node.status != previous_status:
                db.add(
                    HealthEvent(
                        node_id=node.id,
                        from_status=previous_status.value,
                        to_status=node.status.value,
                        reason="heartbeat",
                    )
                )
                await db.flush()
                logger.info(
                    "node_status_changed",
                    extra={"node_id": node.id, "from": previous_status.value, "to": node.status.value},
                )

    def _handle_telemetry_stub(self, telemetry: pb.TelemetryReport) -> None:
        # TODO(usage-ingestion slice): persist telemetry.counters into UsageStat rows
        # (TZ-5 §4.4, §6.4 - byte counters only, never destination/content).
        logger.info(
            "telemetry_received_stub",
            extra={"node_id": telemetry.node_id, "counters": len(telemetry.counters)},
        )

    def _handle_command_result_stub(self, result: pb.CommandResult) -> None:
        # TODO(command-dispatch slice): correlate with the originating Command (once
        # dispatch_command below is implemented) and update RotationRecord/Cascade state.
        logger.info(
            "command_result_received_stub",
            extra={"command_id": result.command_id, "success": result.success},
        )

    def _handle_test_result_stub(self, result: pb.TestResult) -> None:
        # TODO(command-dispatch slice): write/complete the corresponding LinkTest row
        # (TZ-5 §4.3) and push it out over WS /events.
        logger.info(
            "test_result_received_stub",
            extra={"test_id": result.test_id, "outcome": result.outcome},
        )


async def dispatch_command(node_id: str, command: pb.Command) -> None:
    """TODO(command-dispatch slice): send `command` to the open bidi stream for
    `node_id`. Requires tracking live per-node response-queues/streams keyed by node_id
    across concurrent Session() calls (e.g. an asyncio.Queue per node registered when
    its stream opens) - deliberately not built in this api-skeleton slice, since there
    is no cascade-assembly logic yet to originate real commands from. See
    platform/README.md "Scope of this slice / TODO".
    """
    raise NotImplementedError("Command dispatch to Agents is not implemented in this slice.")
