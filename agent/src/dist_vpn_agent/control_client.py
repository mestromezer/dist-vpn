"""Agent<->Platform control-plane gRPC client.

Implements the single ``AgentControl.Session`` bidi-streaming RPC defined in
``contracts/proto/agent_control.proto`` (see that file and TZ-4 SS4/SS5.1).

Design constraints from TZ-4, followed here:

- The Agent *always* dials out to Platform. It never accepts inbound
  control-plane connections (SS5.1, reverse-tunnel/Teleport-style pattern).
- The connection is mTLS by default. An "insecure" plaintext channel exists
  only for local development, is never the default, and is loudly logged as
  unsafe whenever used (SS5.2). The very first bootstrap enrollment (no
  node-specific client cert yet) is server-authenticated TLS only, with the
  one-time enrollment token as the application-layer credential -- see
  config.AgentSettings for why client cert/key are optional there.
- No secret (enrollment token, certificate/key material) is ever logged in
  full (SS5.3).

Scope of *this* slice: enrollment, heartbeat, ping handling, and echoing a
"not implemented" ``CommandResult`` back for any ``Command`` so the full
contract round-trip is exercised end-to-end. Actually dispatching commands to
the local Xray-core, real Docker/Xray health data, and persisting/rotating
the mTLS certificate issued via ``EnrollmentAck`` are out of scope here --
see agent/README.md "Scope of this slice" for the complete list.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import grpc

from dist_vpn_agent.config import AgentSettings, NodeRole, mask_secret
from dist_vpn_agent.generated import agent_control_pb2 as pb2
from dist_vpn_agent.generated import agent_control_pb2_grpc as pb2_grpc

logger = logging.getLogger(__name__)

_ROLE_TO_PROTO: dict[NodeRole, int] = {
    NodeRole.UNSPECIFIED: pb2.NODE_ROLE_UNSPECIFIED,
    NodeRole.ENTRY: pb2.NODE_ROLE_ENTRY,
    NodeRole.EXIT: pb2.NODE_ROLE_EXIT,
}

# Sentinel placed on the outbound queue to end the request stream cleanly.
_END_OF_STREAM = None


class EnrollmentRejected(RuntimeError):
    """Raised when Platform responds to Enrollment with accepted=false.

    Retrying immediately with the same (single-use) token would not help, so
    callers should treat this as terminal for the current token rather than
    something to blindly reconnect-and-retry.
    """


class ControlClient:
    """Owns one dial-out session (and, via :meth:`run_forever`, reconnects).

    ``node_id`` is populated once Platform accepts enrollment and is used on
    subsequent Heartbeats. Nothing is persisted to disk yet in this slice --
    a fresh process starts with ``node_id=None`` and must re-enroll with a
    valid token. See agent/README.md for what's deferred to a follow-up
    slice (persisting the mTLS cert from EnrollmentAck to avoid needing the
    token again).
    """

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        self.node_id: str | None = None
        self._node_id_event = asyncio.Event()

    # -- channel / credentials -------------------------------------------------

    def _build_channel(self) -> grpc.aio.Channel:
        if self._settings.insecure_dev_mode:
            logger.warning(
                "insecure_dev_mode=true: control-plane channel to %s is UNENCRYPTED "
                "and UNAUTHENTICATED. This is a dev-only escape hatch and is unsafe "
                "for production (TZ-4 SS5.2).",
                self._settings.platform_address,
            )
            return grpc.aio.insecure_channel(self._settings.platform_address)

        ca_cert = (
            self._settings.mtls_ca_cert_path.read_bytes()
            if self._settings.mtls_ca_cert_path
            else None
        )
        client_key = (
            self._settings.mtls_client_key_path.read_bytes()
            if self._settings.mtls_client_key_path
            else None
        )
        client_cert = (
            self._settings.mtls_client_cert_path.read_bytes()
            if self._settings.mtls_client_cert_path
            else None
        )
        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca_cert,
            private_key=client_key,
            certificate_chain=client_cert,
        )
        if client_cert is not None:
            logger.info(
                "opening mTLS channel to %s (ca=%s, client_cert=%s, client_key=%s)",
                self._settings.platform_address,
                self._settings.mtls_ca_cert_path,
                self._settings.mtls_client_cert_path,
                self._settings.mtls_client_key_path,
            )
        else:
            logger.info(
                "opening server-authenticated TLS channel to %s (ca=%s, no client "
                "certificate yet -- bootstrap enrollment via one-time token)",
                self._settings.platform_address,
                self._settings.mtls_ca_cert_path,
            )
        return grpc.aio.secure_channel(self._settings.platform_address, credentials)

    # -- outgoing message builders ---------------------------------------------

    def _build_enrollment_message(self) -> pb2.AgentMessage:
        token = self._settings.enrollment_token.get_secret_value()
        logger.info(
            "sending enrollment: hostname=%s agent_version=%s requested_role=%s token=%s",
            self._settings.hostname,
            self._settings.agent_version,
            self._settings.requested_role.value,
            mask_secret(token),
        )
        return pb2.AgentMessage(
            enrollment=pb2.Enrollment(
                enrollment_token=token,
                hostname=self._settings.hostname,
                agent_version=self._settings.agent_version,
                requested_role=_ROLE_TO_PROTO[self._settings.requested_role],
            )
        )

    def _build_heartbeat_message(self) -> pb2.AgentMessage:
        # Real Docker/Xray health reporting lands with the Xray-management
        # slice; this slice only exercises the control-plane round trip, so
        # health is a placeholder.
        return pb2.AgentMessage(
            heartbeat=pb2.Heartbeat(
                node_id=self.node_id or "",
                health=pb2.NodeHealth(
                    xray_container_running=False,
                    cpu_percent=0.0,
                    mem_percent=0.0,
                    disk_percent=0.0,
                ),
                timestamp_unix=int(time.time()),
            )
        )

    # -- incoming message handling ----------------------------------------------

    async def _handle_platform_message(
        self, message: pb2.PlatformMessage, outbound: "asyncio.Queue[pb2.AgentMessage | None]"
    ) -> None:
        kind = message.WhichOneof("payload")

        if kind == "enrollment_ack":
            ack = message.enrollment_ack
            if not ack.accepted:
                raise EnrollmentRejected(ack.reason or "Platform rejected enrollment (no reason given)")
            self.node_id = ack.node_id
            logger.info("enrollment accepted: node_id=%s", ack.node_id)
            if ack.mtls_client_cert_pem:
                logger.info(
                    "Platform issued an mTLS client certificate (%d bytes); "
                    "persisting it and using it for future reconnects instead of the "
                    "enrollment token is not yet implemented in this slice",
                    len(ack.mtls_client_cert_pem),
                )
            self._node_id_event.set()

        elif kind == "ping":
            logger.debug("received ping timestamp_unix=%s", message.ping.timestamp_unix)

        elif kind == "command":
            cmd = message.command
            op = cmd.WhichOneof("op")
            logger.info("received command id=%s op=%s, dispatch not yet implemented in this slice", cmd.command_id, op)
            await outbound.put(
                pb2.AgentMessage(
                    command_result=pb2.CommandResult(
                        command_id=cmd.command_id,
                        success=False,
                        error_message="not implemented in this slice",
                    )
                )
            )

        else:
            logger.warning("received PlatformMessage with no payload set")

    # -- loops -------------------------------------------------------------------

    @staticmethod
    async def _request_iterator(outbound: "asyncio.Queue[pb2.AgentMessage | None]"):
        while True:
            message = await outbound.get()
            if message is _END_OF_STREAM:
                return
            yield message

    async def _read_loop(
        self, call: "grpc.aio.StreamStreamCall", outbound: "asyncio.Queue[pb2.AgentMessage | None]"
    ) -> None:
        async for platform_message in call:
            await self._handle_platform_message(platform_message, outbound)

    async def _heartbeat_loop(self, outbound: "asyncio.Queue[pb2.AgentMessage | None]") -> None:
        # Heartbeat.node_id is meaningless before Platform has told us our
        # node_id, so wait for that before sending the first one.
        await self._node_id_event.wait()
        while True:
            await asyncio.sleep(self._settings.heartbeat_interval_seconds)
            await outbound.put(self._build_heartbeat_message())
            logger.debug("sent heartbeat node_id=%s", self.node_id)

    # -- public API ----------------------------------------------------------

    async def run_session(self) -> None:
        """Dial Platform once, enroll, and run heartbeat+read loops until the
        stream ends (cleanly or with an error). Raises on error; callers that
        want automatic reconnect should use :meth:`run_forever` instead.
        """
        self.node_id = None
        self._node_id_event = asyncio.Event()

        async with self._build_channel() as channel:
            stub = pb2_grpc.AgentControlStub(channel)
            outbound: "asyncio.Queue[pb2.AgentMessage | None]" = asyncio.Queue()
            await outbound.put(self._build_enrollment_message())

            call = stub.Session(self._request_iterator(outbound))
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(outbound))
            try:
                await self._read_loop(call, outbound)
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                await outbound.put(_END_OF_STREAM)

    async def run_forever(self) -> None:
        """Run :meth:`run_session` in a loop with exponential backoff.

        This is what gives the Agent its "restart-safe" property at the
        transport level (TZ-4 SS3.1/SS4): a transient network partition or
        Platform restart causes a reconnect, not a crash. Full declarative
        reconciliation on reconnect (re-fetching desired Xray state) is out
        of scope for this slice -- there is no Xray control yet.
        """
        backoff = self._settings.reconnect_backoff_initial_seconds
        while True:
            try:
                await self.run_session()
                # Clean stream end (server closed it) -- reset backoff and
                # try again immediately.
                backoff = self._settings.reconnect_backoff_initial_seconds
            except asyncio.CancelledError:
                raise
            except EnrollmentRejected:
                logger.exception("enrollment rejected by Platform; not retrying with this token")
                raise
            except Exception:  # noqa: BLE001 - top-level supervisor, log and retry
                logger.exception(
                    "control-plane session ended unexpectedly; reconnecting in %.1fs",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._settings.reconnect_backoff_max_seconds)
