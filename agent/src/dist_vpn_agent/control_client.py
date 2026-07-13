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

Scope of *this* slice: enrollment, heartbeat, ping handling, and real
dispatch of six ``Command`` operations to the local Xray-core via
``xray_client.py`` (add/remove inbound/outbound, add/remove user,
add/remove routing rule, override balancer) -- see ``_COMMAND_DISPATCH``
below. ``run_test`` and ``rotate_secret`` remain unimplemented (see
agent/README.md "Scope of this slice" for why), and persisting/rotating the
mTLS certificate issued via ``EnrollmentAck`` is still out of scope here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

import grpc

from dist_vpn_agent.config import AgentSettings, NodeRole, mask_secret
from dist_vpn_agent.docker_manager import DockerManager
from dist_vpn_agent.generated import agent_control_pb2 as pb2
from dist_vpn_agent.generated import agent_control_pb2_grpc as pb2_grpc
from dist_vpn_agent.xray_client import XrayClient, XrayClientError

logger = logging.getLogger(__name__)

# Maps a Command.op oneof field name to a function that carries it out
# against a real, local Xray-core (see xray_client.py and TZ-4 SS3.2's
# operation-to-Xray-RPC table). Deliberately does NOT include "run_test" or
# "rotate_secret" -- see agent/README.md "Scope of this slice" for why those
# two remain unimplemented; any op not present here falls through to the
# NotImplementedError branch in _handle_command below.
_COMMAND_DISPATCH: dict[str, Callable[[XrayClient, pb2.Command], Awaitable[None]]] = {
    "add_inbound": lambda client, cmd: client.add_inbound(cmd.add_inbound.inbound_json),
    "remove_inbound": lambda client, cmd: client.remove_inbound(cmd.remove_inbound.tag),
    "add_outbound": lambda client, cmd: client.add_outbound(cmd.add_outbound.outbound_json),
    "remove_outbound": lambda client, cmd: client.remove_outbound(cmd.remove_outbound.tag),
    "add_user": lambda client, cmd: client.add_user(
        cmd.add_user.inbound_tag, cmd.add_user.uuid, cmd.add_user.email, cmd.add_user.flow
    ),
    "remove_user": lambda client, cmd: client.remove_user(
        cmd.remove_user.inbound_tag, cmd.remove_user.email
    ),
    "add_routing_rule": lambda client, cmd: client.add_routing_rule(
        cmd.add_routing_rule.rule_tag, cmd.add_routing_rule.rule_json
    ),
    "remove_routing_rule": lambda client, cmd: client.remove_routing_rule(
        cmd.remove_routing_rule.rule_tag
    ),
    "override_balancer": lambda client, cmd: client.override_balancer(
        cmd.override_balancer.balancer_tag, cmd.override_balancer.target_outbound_tag
    ),
}

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

    def __init__(
        self,
        settings: AgentSettings,
        xray_client: XrayClient | None = None,
        docker_manager: DockerManager | None = None,
    ) -> None:
        self._settings = settings
        self.node_id: str | None = None
        self._node_id_event = asyncio.Event()
        # Overridable for tests (point it at an in-process mock Xray-core,
        # see tests/mock_xray_server.py); defaults to the real local
        # Xray-core loopback address from settings.
        self._xray_client = xray_client if xray_client is not None else XrayClient(settings.xray_api_address)
        # Overridable for tests (inject a fake/mock docker client via
        # DockerManager(settings, client=...)); defaults to real Docker
        # Engine API access. Used only to read health for the heartbeat --
        # see main.py for where ensure_running() is actually called.
        self._docker_manager = docker_manager if docker_manager is not None else DockerManager(settings)

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

    async def _build_heartbeat_message(self) -> pb2.AgentMessage:
        # docker_manager.health() talks to the (blocking, synchronous)
        # docker-py client -- run it off the event loop thread so a slow or
        # wedged Docker Engine API can't stall the heartbeat/read loops.
        health = await asyncio.to_thread(self._docker_manager.health)
        if health.status == "docker_unavailable":
            logger.warning("heartbeat: Docker Engine API unreachable: %s", health.detail)
        # cpu_percent/mem_percent/disk_percent remain placeholders -- see
        # agent/README.md "Scope of this slice" (host resource sampling,
        # e.g. via psutil, is a separate, not-yet-made dependency decision).
        return pb2.AgentMessage(
            heartbeat=pb2.Heartbeat(
                node_id=self.node_id or "",
                health=pb2.NodeHealth(
                    xray_container_running=health.running,
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
            await self._handle_command(message.command, outbound)

        else:
            logger.warning("received PlatformMessage with no payload set")

    async def _handle_command(
        self, cmd: pb2.Command, outbound: "asyncio.Queue[pb2.AgentMessage | None]"
    ) -> None:
        """Dispatch one ``Command`` to the local Xray-core (via
        ``xray_client.py``) and report back a real ``CommandResult`` --
        never the placeholder "not implemented" echo from the previous
        slice, except for the two ops genuinely not built yet (``run_test``,
        ``rotate_secret``; see agent/README.md).

        Any failure -- a real ``grpc.RpcError`` from Xray, a request-
        translation error (``XrayClientError``), or something unexpected --
        is logged (never swallowed silently) and turned into
        ``CommandResult(success=False, error_message=...)`` so Platform sees
        exactly what went wrong, not just a generic failure.
        """
        op = cmd.WhichOneof("op")
        handler = _COMMAND_DISPATCH.get(op)

        try:
            if handler is None:
                raise NotImplementedError(
                    f"Command op {op!r} is not implemented in this slice (see agent/README.md "
                    "'Scope of this slice')"
                )
            await handler(self._xray_client, cmd)
        except (XrayClientError, NotImplementedError) as err:
            logger.error("command id=%s op=%s failed: %s", cmd.command_id, op, err)
            result = pb2.CommandResult(command_id=cmd.command_id, success=False, error_message=str(err))
        except Exception:  # noqa: BLE001 - last-resort guard so one bad command can't kill the session
            logger.exception(
                "command id=%s op=%s failed with an unexpected internal error", cmd.command_id, op
            )
            result = pb2.CommandResult(
                command_id=cmd.command_id,
                success=False,
                error_message=f"unexpected internal Agent error handling {op}",
            )
        else:
            logger.info("command id=%s op=%s succeeded", cmd.command_id, op)
            result = pb2.CommandResult(command_id=cmd.command_id, success=True)

        await outbound.put(pb2.AgentMessage(command_result=result))

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
            await outbound.put(await self._build_heartbeat_message())
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
        reconciliation on reconnect (Platform re-pushing the complete desired
        Xray state after a gap, rather than the Agent only reacting to
        Commands it happens to receive) is still out of scope for this
        slice -- see agent/README.md.
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
