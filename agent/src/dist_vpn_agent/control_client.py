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

Scope of *this* slice: enrollment (now with a real locally-generated CSR,
see identity.py), persisted-identity reconnect, heartbeat, ping handling,
real dispatch of nine ``Command`` operations to the local Xray-core via
``xray_client.py`` (add/remove inbound, add/remove outbound, add/remove
user, add/remove routing rule, override balancer) -- see
``_COMMAND_DISPATCH`` below -- and ``run_test`` (real ephemeral-client
connectivity probing, see ``connectivity_test.py`` and ``_handle_run_test``
below). ``rotate_secret`` remains unimplemented (see agent/README.md "Scope
of this slice" for why).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

import grpc
import psutil

from dist_vpn_agent import identity
from dist_vpn_agent.config import AgentSettings, NodeRole, mask_secret
from dist_vpn_agent.connectivity_test import ConnectivityTester
from dist_vpn_agent.docker_manager import DockerManager
from dist_vpn_agent.generated import agent_control_pb2 as pb2
from dist_vpn_agent.generated import agent_control_pb2_grpc as pb2_grpc
from dist_vpn_agent.xray_client import XrayClient, XrayClientError

logger = logging.getLogger(__name__)

# Maps a Command.op oneof field name to a function that carries it out
# against a real, local Xray-core (see xray_client.py and TZ-4 SS3.2's
# operation-to-Xray-RPC table). Deliberately does NOT include "run_test"
# (handled entirely separately -- see _handle_command/_handle_run_test, it
# replies with a TestResult, not a CommandResult, so it can't be a same-
# shaped entry in this table) or "rotate_secret" -- see agent/README.md
# "Scope of this slice" for why that one remains unimplemented; any op not
# present here (and not "run_test", which is intercepted before this table
# is even consulted) falls through to the NotImplementedError branch in
# _handle_command below.
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

    ``node_id`` is populated as soon as this Agent knows who it is: either
    from a persisted identity (``identity.py``, checked at the top of every
    :meth:`run_session`) or, on first-ever bootstrap, from Platform's
    ``EnrollmentAck``. See :meth:`run_session` for the full "persisted
    identity vs. bootstrap-enroll" decision and agent/README.md for the
    reconnect-signal design this implements (the sibling Platform-track
    session building the CA/signing side needs to know about it).
    """

    def __init__(
        self,
        settings: AgentSettings,
        xray_client: XrayClient | None = None,
        docker_manager: DockerManager | None = None,
        connectivity_tester: ConnectivityTester | None = None,
    ) -> None:
        self._settings = settings
        self.node_id: str | None = None
        self._node_id_event = asyncio.Event()
        # In-memory-only private key + CSR for the *current* bootstrap
        # enrollment attempt, if one is in flight this session -- see
        # _build_enrollment_message and _handle_platform_message's
        # enrollment_ack branch. Never written to disk until (if) a matching
        # EnrollmentAck.mtls_client_cert_pem comes back (identity.py
        # persist_identity); None whenever this session reconnected with an
        # already-persisted identity instead (no bootstrap attempt at all).
        self._generated_csr: identity.GeneratedCsr | None = None
        # Overridable for tests (point it at an in-process mock Xray-core,
        # see tests/mock_xray_server.py); defaults to the real local
        # Xray-core loopback address from settings.
        self._xray_client = xray_client if xray_client is not None else XrayClient(settings.xray_api_address)
        # Overridable for tests (inject a fake/mock docker client via
        # DockerManager(settings, client=...)); defaults to real Docker
        # Engine API access. Used only to read health for the heartbeat --
        # see main.py for where ensure_running() is actually called.
        self._docker_manager = docker_manager if docker_manager is not None else DockerManager(settings)
        # Overridable for tests (inject a ConnectivityTester with a fake/mock
        # docker client, or a stand-in with a scripted run_test()); defaults
        # to real Docker Engine API access for ephemeral test containers --
        # see connectivity_test.py.
        self._connectivity_tester = (
            connectivity_tester if connectivity_tester is not None else ConnectivityTester(settings)
        )

    # -- channel / credentials -------------------------------------------------

    def _build_channel(self, persisted: identity.PersistedIdentity | None) -> grpc.aio.Channel:
        """``persisted``, when not ``None``, wins over
        ``settings.mtls_client_cert_path``/``mtls_client_key_path`` -- a
        self-generated-and-Platform-issued identity from a previous
        successful enrollment always supersedes the operator-supplied
        override paths (which remain meant for identities provisioned
        entirely out-of-band, outside this CSR flow -- see identity.py
        module docstring point 3 and agent/README.md).
        """
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
        if persisted is not None:
            client_key = persisted.key_path.read_bytes()
            client_cert = persisted.cert_path.read_bytes()
        else:
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
        if persisted is not None:
            logger.info(
                "opening mTLS channel to %s using persisted node identity (node_id=%s, "
                "cert=%s, key=%s)",
                self._settings.platform_address,
                persisted.node_id,
                persisted.cert_path,
                persisted.key_path,
            )
        elif client_cert is not None:
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
        """Bootstrap enrollment: generate a fresh local EC/P-256 keypair +
        PKCS#10 CSR (identity.py) and send the CSR PEM in
        ``Enrollment.csr_pem``. The private key is kept only in memory
        (``self._generated_csr``) until (if) a matching
        ``EnrollmentAck.mtls_client_cert_pem`` arrives -- see
        ``_handle_platform_message``. A fresh keypair is generated on every
        call (i.e. every bootstrap attempt, including retries after a
        dropped connection before ack) rather than reused across attempts --
        simpler and safer than trying to detect "is this a retry of the
        exact same attempt", at the cost of one throwaway keypair per failed
        attempt, which is cheap for EC/P-256.
        """
        token = self._settings.enrollment_token.get_secret_value()
        self._generated_csr = identity.generate_csr(common_name=self._settings.hostname)
        logger.info(
            "sending enrollment: hostname=%s agent_version=%s requested_role=%s token=%s "
            "csr=%d bytes",
            self._settings.hostname,
            self._settings.agent_version,
            self._settings.requested_role.value,
            mask_secret(token),
            len(self._generated_csr.csr_pem),
        )
        return pb2.AgentMessage(
            enrollment=pb2.Enrollment(
                enrollment_token=token,
                hostname=self._settings.hostname,
                agent_version=self._settings.agent_version,
                requested_role=_ROLE_TO_PROTO[self._settings.requested_role],
                csr_pem=self._generated_csr.csr_pem,
            )
        )

    def _sample_host_resources(self) -> tuple[float, float, float]:
        """Real cpu/mem/disk percentages via psutil - was a hardcoded (0.0, 0.0, 0.0)
        placeholder until this slice (see agent/README.md "Scope of this slice",
        closed while wiring resource-vs-user-count monitoring on the Platform side,
        which needs real numbers here to mean anything).

        ``cpu_percent(interval=None)`` is Xray-core's own non-blocking convention
        (instantaneous rate since the *previous* call, not a fresh blocking sample) -
        the first call in a process's lifetime always returns 0.0, which is expected
        and self-corrects from the second heartbeat onward, not a bug.

        ``disk_usage("/")`` reports the Agent process's own filesystem view - inside
        the Agent's container this is the container's root (mostly the image layers
        plus whatever's bind-mounted at "/"), which approximates but is not
        identical to the *host* VDS's true disk pressure across all containers.
        A known, documented simplification, not a precise host-wide metric.
        """
        return (
            psutil.cpu_percent(interval=None),
            psutil.virtual_memory().percent,
            psutil.disk_usage("/").percent,
        )

    async def _build_heartbeat_message(self) -> pb2.AgentMessage:
        # docker_manager.health() talks to the (blocking, synchronous)
        # docker-py client -- run it off the event loop thread so a slow or
        # wedged Docker Engine API can't stall the heartbeat/read loops.
        health = await asyncio.to_thread(self._docker_manager.health)
        if health.status == "docker_unavailable":
            logger.warning("heartbeat: Docker Engine API unreachable: %s", health.detail)
        cpu_percent, mem_percent, disk_percent = await asyncio.to_thread(self._sample_host_resources)
        return pb2.AgentMessage(
            heartbeat=pb2.Heartbeat(
                node_id=self.node_id or "",
                health=pb2.NodeHealth(
                    xray_container_running=health.running,
                    cpu_percent=cpu_percent,
                    mem_percent=mem_percent,
                    disk_percent=disk_percent,
                ),
                timestamp_unix=int(time.time()),
            )
        )

    async def _build_telemetry_message(self) -> pb2.AgentMessage | None:
        """Real per-user traffic + online-device telemetry via
        ``xray_client.get_users_stats()`` (StatsService) - previously nothing built
        or sent a ``TelemetryReport`` at all (the message type existed in the
        contract and Platform's ingestion side was tested against synthetic data,
        but no real Agent code path ever populated one). Returns ``None`` (skip
        this cycle, don't crash the heartbeat loop) if the local Xray-core API is
        unreachable - the same "a stat we can't read isn't worth losing the whole
        control-plane session over" reasoning as ``_build_heartbeat_message``'s
        Docker-health handling above, just without a health-status field to report
        the failure through (TelemetryReport has no room for a partial-failure
        signal in the contract - logged instead)."""
        try:
            user_stats = await self._xray_client.get_users_stats()
        except XrayClientError as err:
            logger.warning("telemetry: local Xray-core StatsService unreachable: %s", err)
            return None

        counters = [
            pb2.UsageCounter(
                scope="user",
                key=stat.email,
                uplink_bytes=stat.uplink_bytes,
                downlink_bytes=stat.downlink_bytes,
            )
            for stat in user_stats
            if stat.uplink_bytes or stat.downlink_bytes
        ]
        user_online = [
            pb2.UserOnlineStat(email=stat.email, online_ips=stat.online_ips)
            for stat in user_stats
            if stat.online_ips
        ]
        if not counters and not user_online:
            return None

        return pb2.AgentMessage(
            telemetry=pb2.TelemetryReport(
                node_id=self.node_id or "",
                counters=counters,
                user_online=user_online,
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
                if self._generated_csr is None:
                    # Should not happen in practice (an EnrollmentAck implies
                    # this session sent an Enrollment, which always sets
                    # _generated_csr first, see _build_enrollment_message) --
                    # guarded rather than assumed so a future protocol change
                    # can't turn this into persisting a cert with no matching
                    # key on disk.
                    logger.warning(
                        "received a non-empty mtls_client_cert_pem but no local CSR/private "
                        "key is in memory for this session; cannot persist an identity "
                        "without its matching private key, skipping persistence"
                    )
                else:
                    # OSError (permissions, read-only filesystem, disk full,
                    # ...) is caught here and only logged -- enrollment
                    # already *succeeded* server-side by this point (the
                    # one-time token is already consumed regardless of what
                    # happens locally next), so this session should carry on
                    # using the in-memory node_id/identity rather than treat
                    # a local disk-write failure as fatal to the whole
                    # control-plane session. The real, accepted cost of this
                    # failing: the next restart/reconnect finds no persisted
                    # identity and falls back to bootstrap-enrollment again
                    # -- which needs a *fresh* token, since this one is
                    # already spent (TZ-4 SS5.2, single-use). Operationally
                    # surprising if it happens, but not a reason to crash a
                    # session that is otherwise working -- see agent/README.md.
                    try:
                        identity.persist_identity(
                            self._settings.state_dir,
                            node_id=ack.node_id,
                            private_key_pem=self._generated_csr.private_key_pem,
                            cert_pem=ack.mtls_client_cert_pem,
                        )
                    except OSError:
                        logger.exception(
                            "enrollment succeeded (node_id=%s) but persisting the issued "
                            "identity to %s failed; this session continues normally, but "
                            "the next restart/reconnect will have to bootstrap-enroll again "
                            "with a fresh enrollment_token (this one is now spent) -- check "
                            "that state_dir is writable by this process",
                            ack.node_id,
                            self._settings.state_dir,
                        )
                    else:
                        logger.info(
                            "persisted new mTLS client identity to %s (%d-byte certificate); "
                            "subsequent restarts/reconnects will reuse it instead of "
                            "re-enrolling with the token",
                            self._settings.state_dir,
                            len(ack.mtls_client_cert_pem),
                        )
            else:
                logger.warning(
                    "EnrollmentAck.accepted=true but mtls_client_cert_pem is empty -- this "
                    "Agent has no persistable identity and will enroll with the token again "
                    "on the next reconnect"
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
        slice, except for the one op genuinely not built yet
        (``rotate_secret``; see agent/README.md).

        ``run_test`` is handled entirely separately (:meth:`_handle_run_test`)
        and does *not* fall through to this method's ``CommandResult`` path
        at all -- see that method's docstring for why.

        Any failure -- a real ``grpc.RpcError`` from Xray, a request-
        translation error (``XrayClientError``), or something unexpected --
        is logged (never swallowed silently) and turned into
        ``CommandResult(success=False, error_message=...)`` so Platform sees
        exactly what went wrong, not just a generic failure.
        """
        op = cmd.WhichOneof("op")

        if op == "run_test":
            await self._handle_run_test(cmd, outbound)
            return

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

    async def _handle_run_test(
        self, cmd: pb2.Command, outbound: "asyncio.Queue[pb2.AgentMessage | None]"
    ) -> None:
        """Design decision (not spelled out by the contract, see
        connectivity_test.py module docstring and agent/README.md
        "Замеченные вопросы к контракту" -- flagged for the Platform side to
        reconcile against): a ``Command`` whose ``op`` is ``run_test`` gets
        **only** an ``AgentMessage.test_result`` reply, correlated by
        ``RunTest.test_id`` -- never an ``AgentMessage.command_result`` for
        ``cmd.command_id``. The contract defines ``TestResult`` as a sibling
        top-level ``AgentMessage.payload`` variant (not nested under
        ``CommandResult``) and keys it by its own ``test_id`` rather than
        reusing ``Command.command_id``, which this Agent reads as a
        deliberate signal that Platform correlates test outcomes
        independently of the command channel (e.g. for polling/history by
        test_id, TZ-5's admin-facing "test history" concept) rather than
        wanting a redundant ok/fail ``CommandResult`` on top. Platform's own
        ``Session`` handler must not block waiting for a ``CommandResult``
        that will never come for this specific op.

        Runs the actual probe via ``ConnectivityTester.run_test`` in a
        worker thread (``asyncio.to_thread``, docker-py and ``requests`` are
        both synchronous) so a slow/wedged probe can't stall this session's
        heartbeat or read loop -- same reasoning as
        ``_build_heartbeat_message``'s use of ``asyncio.to_thread`` for
        ``docker_manager.health()``.
        """
        run_test = cmd.run_test
        logger.info(
            "command id=%s op=run_test test_id=%s probe_url=%s timeout_seconds=%d: starting "
            "ephemeral connectivity probe",
            cmd.command_id,
            run_test.test_id,
            run_test.probe_url,
            run_test.timeout_seconds,
        )
        try:
            result = await asyncio.to_thread(self._connectivity_tester.run_test, run_test)
        except Exception:  # noqa: BLE001 - last-resort guard, mirrors _handle_command's own
            logger.exception(
                "run_test command id=%s test_id=%s failed with an unexpected internal error "
                "(ConnectivityTester.run_test is expected to catch everything it knows about "
                "and never raise -- this is a bug if it triggers)",
                cmd.command_id,
                run_test.test_id,
            )
            result = pb2.TestResult(
                test_id=run_test.test_id,
                outcome=pb2.TEST_OUTCOME_UNSPECIFIED,
                latency_ms=0,
                detail="unexpected internal Agent error running the connectivity test",
            )

        logger.info(
            "run_test test_id=%s outcome=%s latency_ms=%d detail=%s",
            result.test_id,
            pb2.TestOutcome.Name(result.outcome),
            result.latency_ms,
            result.detail,
        )
        await outbound.put(pb2.AgentMessage(test_result=result))

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

            telemetry_message = await self._build_telemetry_message()
            if telemetry_message is not None:
                await outbound.put(telemetry_message)
                logger.debug("sent telemetry node_id=%s", self.node_id)

    # -- public API ----------------------------------------------------------

    async def run_session(self) -> None:
        """Dial Platform once and run heartbeat+read loops until the stream
        ends (cleanly or with an error). Raises on error; callers that want
        automatic reconnect should use :meth:`run_forever` instead.

        --- Persisted identity vs. bootstrap-enroll, and the "Heartbeat-first
            reconnect" signal (read this if you are the sibling session
            building Platform's CA/signing side and can't see this file) ---

        Every call checks ``identity.load_persisted_identity(settings.state_dir)``
        first:

        - **Persisted identity found** (a previous successful enrollment on
          this node, this process start *or* an earlier one): the channel is
          built with that cert/key as the mTLS client identity
          (:meth:`_build_channel`), ``self.node_id`` is set immediately from
          the persisted ``node_id`` (no need to ask Platform who we are --
          we already know), and the **first message put on the wire is a
          ``Heartbeat``, not an ``Enrollment``** -- no ``Enrollment`` is sent
          at all this session. This is the reconnect signal: Platform can
          tell "this is a returning, already-known node" from "this is a
          bootstrap or explicit re-enrollment" purely by which message type
          opens the stream, with no new field needed on either message.
          The actual trust anchor is still the mTLS client certificate
          itself (verified at the transport layer against Platform's CA
          before any application-level message is even read) -- the
          Heartbeat's ``node_id`` is what Platform's ``Session`` handler
          uses to look up this node's state, but a Platform implementation
          should still cross-check it against whatever node identity its
          own mTLS layer associated with the client certificate, exactly as
          it presumably already must for every subsequent message on an
          established mTLS connection, not trust the field blindly.
        - **No persisted identity** (first-ever boot, or a wiped state
          directory): unchanged bootstrap path -- a fresh EC/P-256
          keypair+CSR is generated and an ``Enrollment`` (carrying the CSR
          and the one-time token) opens the stream instead, over a channel
          that is TLS (server-authenticated only, no client cert yet -- see
          :meth:`_build_channel`) rather than full mTLS.
        """
        self.node_id = None
        self._node_id_event = asyncio.Event()
        self._generated_csr = None

        persisted_identity = identity.load_persisted_identity(self._settings.state_dir)

        async with self._build_channel(persisted_identity) as channel:
            stub = pb2_grpc.AgentControlStub(channel)
            outbound: "asyncio.Queue[pb2.AgentMessage | None]" = asyncio.Queue()

            if persisted_identity is not None:
                self.node_id = persisted_identity.node_id
                self._node_id_event.set()
                logger.info(
                    "reusing persisted mTLS identity (node_id=%s) from %s -- skipping "
                    "Enrollment, sending an immediate Heartbeat instead (reconnect signal, "
                    "see run_session docstring)",
                    persisted_identity.node_id,
                    self._settings.state_dir,
                )
                await outbound.put(await self._build_heartbeat_message())
            else:
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
