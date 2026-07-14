"""End-to-end test: ControlClient against an in-process mock AgentControl
server, covering enrollment -> heartbeat -> ping -> command round trip
(TZ-4 scope for this slice; see agent/README.md).

The command-dispatch tests below run the *full* stack: a mock Platform
(mock_platform_server) sends a Command over the Agent<->Platform session,
ControlClient dispatches it through xray_client.py to a mock local Xray-core
(mock_xray_server) and reports the real CommandResult back over the same
session -- as close to the real end-to-end path as this sandboxed test
environment (no real Xray-core binary) allows. See the final report on this
slice for exactly what is/isn't verified against a *real* Xray-core.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path

import grpc
import pytest

from dist_vpn_agent.config import AgentSettings, NodeRole
from dist_vpn_agent.control_client import ControlClient, EnrollmentRejected
from dist_vpn_agent.docker_manager import XrayContainerHealth
from dist_vpn_agent.generated import agent_control_pb2 as pb2
from dist_vpn_agent.xray_client import XrayClient
from mock_platform_server import MockAgentControlServicer, running
from mock_xray_server import MockHandlerServicer
from mock_xray_server import running as xray_running


def make_settings(address: str, **overrides) -> AgentSettings:
    # state_dir: a fresh, isolated temp directory per call unless a test
    # explicitly overrides it (e.g. to test persisted-identity reconnect
    # across two ControlClient instances that must share one). This is not
    # optional plumbing -- AgentSettings.state_dir defaults to
    # /var/lib/dist-vpn-agent, a real, persistent path; leaving it unset
    # here would make every one of these enrollment-round-trip tests read
    # and write that real directory, and once one test's mock enrollment
    # persists an identity there, every *later* test in the same run (or a
    # later run, since it's real disk, not a fixture) would find it, skip
    # sending Enrollment, and hang waiting for a Command the mock server
    # only ever sends in response to one (this was a real, reproduced bug
    # during this slice's development, not a hypothetical -- see the final
    # report for this slice).
    defaults = dict(
        platform_address=address,
        enrollment_token="one-time-token-123",
        hostname="test-host",
        agent_version="0.1.0-test",
        requested_role=NodeRole.ENTRY,
        heartbeat_interval_seconds=0.05,
        insecure_dev_mode=True,
        state_dir=Path(tempfile.mkdtemp(prefix="dist-vpn-agent-test-state-")),
    )
    defaults.update(overrides)
    return AgentSettings(**defaults)


class _FakeDockerManager:
    """Stand-in for DockerManager that never touches a real Docker Engine --
    used by tests below that aren't about Docker/Xray-container health
    specifically, so they stay hermetic and fast regardless of whether this
    environment happens to have Docker running.
    """

    def __init__(self, running: bool = False) -> None:
        self._running = running

    def health(self) -> XrayContainerHealth:
        return XrayContainerHealth(running=self._running, status="running" if self._running else "not_found")

    def is_running(self) -> bool:
        return self._running


@pytest.mark.asyncio
async def test_enrollment_heartbeat_ping_round_trip():
    servicer = MockAgentControlServicer(node_id="node-abc", accept=True)

    async with running(servicer) as address:
        client = ControlClient(make_settings(address), docker_manager=_FakeDockerManager())
        session_task = asyncio.create_task(client.run_session())
        try:
            await asyncio.wait_for(servicer.heartbeat_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task

    assert client.node_id == "node-abc"

    assert len(servicer.received_enrollments) == 1
    enrollment = servicer.received_enrollments[0]
    assert enrollment.enrollment_token == "one-time-token-123"
    assert enrollment.hostname == "test-host"
    assert enrollment.agent_version == "0.1.0-test"
    assert enrollment.requested_role == pb2.NODE_ROLE_ENTRY

    assert len(servicer.received_heartbeats) >= 1
    assert servicer.received_heartbeats[0].node_id == "node-abc"


@pytest.mark.asyncio
@pytest.mark.parametrize("xray_running", [True, False])
async def test_heartbeat_reflects_real_docker_manager_health(xray_running):
    # Heartbeat.health.xray_container_running used to be hardcoded False
    # (this slice's whole point is that it now reflects docker_manager.py's
    # real answer) -- exercise both a running and a not-running container.
    servicer = MockAgentControlServicer(node_id="node-abc", accept=True)

    async with running(servicer) as address:
        client = ControlClient(
            make_settings(address), docker_manager=_FakeDockerManager(running=xray_running)
        )
        session_task = asyncio.create_task(client.run_session())
        try:
            await asyncio.wait_for(servicer.heartbeat_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task

    assert servicer.received_heartbeats[0].health.xray_container_running is xray_running


class _FakeConnectivityTester:
    """Stand-in for ConnectivityTester -- these tests are about
    control_client.py's *wiring* (does a run_test Command reach
    ConnectivityTester.run_test, does the RunTest payload get passed through
    correctly, does the result come back as a TestResult and specifically
    NOT a CommandResult) rather than the real Docker/HTTP probe logic itself
    (real Docker + a mix of real and mocked outcome-mapping logic is covered
    separately, see tests/test_connectivity_test.py).
    """

    def __init__(self, result: pb2.TestResult) -> None:
        self._result = result
        self.received_run_tests: list[pb2.RunTest] = []

    def run_test(self, run_test: pb2.RunTest) -> pb2.TestResult:
        self.received_run_tests.append(run_test)
        return self._result


@pytest.mark.asyncio
async def test_run_test_command_dispatches_to_connectivity_tester_and_returns_test_result():
    # RunTest is now real (this slice) -- unlike the nine xray_client.py-
    # dispatched ops, it must produce a TestResult (correlated by
    # RunTest.test_id), not a CommandResult (see _handle_run_test's
    # docstring for why) -- verify both the dispatch (ConnectivityTester
    # receives the exact RunTest payload) and the reply shape/routing.
    run_test_payload = pb2.RunTest(
        test_id="t1",
        preset_client_config_json='{"protocol": "freedom"}',
        probe_url="https://example.com",
        timeout_seconds=5,
    )
    command = pb2.Command(command_id="cmd-1", run_test=run_test_payload)
    servicer = MockAgentControlServicer(node_id="node-xyz", accept=True, send_command=command)
    fake_result = pb2.TestResult(
        test_id="t1", outcome=pb2.TEST_OUTCOME_SUCCESS, latency_ms=42, detail="HTTP 200 in 42ms"
    )
    fake_tester = _FakeConnectivityTester(fake_result)

    async with running(servicer) as address:
        client = ControlClient(
            make_settings(address),
            docker_manager=_FakeDockerManager(),
            connectivity_tester=fake_tester,
        )
        session_task = asyncio.create_task(client.run_session())
        try:
            await asyncio.wait_for(servicer.test_result_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task

    assert len(fake_tester.received_run_tests) == 1
    assert fake_tester.received_run_tests[0] == run_test_payload

    assert len(servicer.received_test_results) == 1
    result = servicer.received_test_results[0]
    assert result.test_id == "t1"
    assert result.outcome == pb2.TEST_OUTCOME_SUCCESS
    assert result.latency_ms == 42
    assert result.detail == "HTTP 200 in 42ms"

    # The defining behavior this design decision requires (see
    # _handle_run_test docstring): no CommandResult at all for a run_test
    # Command, ever -- Platform must not wait for one.
    assert servicer.received_command_results == []


@pytest.mark.asyncio
async def test_run_test_unexpected_tester_exception_yields_unspecified_test_result():
    # Last-resort guard, mirrors _handle_command's own -- ConnectivityTester
    # is documented to catch everything it knows about and never raise, but
    # if it somehow does, this must still degrade to an honest TestResult
    # (not a crashed session), same spirit as the "unexpected internal
    # error" branch of the other nine ops.
    class _RaisingTester:
        def run_test(self, run_test: pb2.RunTest) -> pb2.TestResult:
            raise RuntimeError("boom")

    command = pb2.Command(command_id="cmd-1", run_test=pb2.RunTest(test_id="t-err", probe_url="https://example.com"))
    servicer = MockAgentControlServicer(node_id="node-xyz", accept=True, send_command=command)

    async with running(servicer) as address:
        client = ControlClient(
            make_settings(address),
            docker_manager=_FakeDockerManager(),
            connectivity_tester=_RaisingTester(),
        )
        session_task = asyncio.create_task(client.run_session())
        try:
            await asyncio.wait_for(servicer.test_result_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task

    result = servicer.received_test_results[0]
    assert result.test_id == "t-err"
    assert result.outcome == pb2.TEST_OUTCOME_UNSPECIFIED
    assert "unexpected internal Agent error" in result.detail

    # No CommandResult either, same as the success path -- an unexpected
    # internal error in run_test still degrades to a TestResult only.
    assert servicer.received_command_results == []


@pytest.mark.asyncio
async def test_add_user_command_dispatches_to_real_xray_client_and_succeeds():
    # Full stack: mock Platform sends AddUser -> ControlClient dispatches
    # through xray_client.py -> mock Xray-core HandlerService.AlterInbound.
    xray_handler = MockHandlerServicer()
    async with xray_running(handler=xray_handler) as xray_address:
        xray_client = XrayClient(xray_address)
        command = pb2.Command(
            command_id="cmd-1",
            add_user=pb2.AddUser(inbound_tag="in1", uuid="u1", email="user@example.com", flow="xtls-rprx-vision"),
        )
        servicer = MockAgentControlServicer(node_id="node-xyz", accept=True, send_command=command)

        async with running(servicer) as platform_address:
            client = ControlClient(
                make_settings(platform_address), xray_client=xray_client, docker_manager=_FakeDockerManager()
            )
            session_task = asyncio.create_task(client.run_session())
            try:
                await asyncio.wait_for(servicer.command_result_received.wait(), timeout=5)
            finally:
                session_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await session_task
        await xray_client.close()

    assert len(xray_handler.alter_inbound_requests) == 1
    assert xray_handler.alter_inbound_requests[0].tag == "in1"

    result = servicer.received_command_results[0]
    assert result.command_id == "cmd-1"
    assert result.success is True
    assert result.error_message == ""


@pytest.mark.asyncio
async def test_add_inbound_command_reports_real_xray_error_not_swallowed():
    # The local Xray-core rejects the request (e.g. duplicate tag) -- the
    # Agent must surface that real error in CommandResult, not swallow it
    # or report a generic message.
    xray_handler = MockHandlerServicer(
        fail_with=grpc.StatusCode.ALREADY_EXISTS, fail_details="inbound tag already exists"
    )
    async with xray_running(handler=xray_handler) as xray_address:
        xray_client = XrayClient(xray_address)
        command = pb2.Command(
            command_id="cmd-2",
            add_inbound=pb2.AddInbound(
                inbound_json=json.dumps({"tag": "dup", "protocol": "vless", "settings": {}})
            ),
        )
        servicer = MockAgentControlServicer(node_id="node-xyz", accept=True, send_command=command)

        async with running(servicer) as platform_address:
            client = ControlClient(
                make_settings(platform_address), xray_client=xray_client, docker_manager=_FakeDockerManager()
            )
            session_task = asyncio.create_task(client.run_session())
            try:
                await asyncio.wait_for(servicer.command_result_received.wait(), timeout=5)
            finally:
                session_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await session_task
        await xray_client.close()

    result = servicer.received_command_results[0]
    assert result.command_id == "cmd-2"
    assert result.success is False
    assert "ALREADY_EXISTS" in result.error_message
    assert "inbound tag already exists" in result.error_message


@pytest.mark.asyncio
async def test_unsupported_protocol_in_add_inbound_reports_translation_error():
    # A translation-layer failure (never reaches Xray at all) must also be
    # surfaced honestly, not treated as a generic/unimplemented failure.
    xray_handler = MockHandlerServicer()
    async with xray_running(handler=xray_handler) as xray_address:
        xray_client = XrayClient(xray_address)
        command = pb2.Command(
            command_id="cmd-3",
            add_inbound=pb2.AddInbound(
                inbound_json=json.dumps({"tag": "x", "protocol": "vmess", "settings": {}})
            ),
        )
        servicer = MockAgentControlServicer(node_id="node-xyz", accept=True, send_command=command)

        async with running(servicer) as platform_address:
            client = ControlClient(
                make_settings(platform_address), xray_client=xray_client, docker_manager=_FakeDockerManager()
            )
            session_task = asyncio.create_task(client.run_session())
            try:
                await asyncio.wait_for(servicer.command_result_received.wait(), timeout=5)
            finally:
                session_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await session_task
        await xray_client.close()

    assert xray_handler.add_inbound_requests == []  # never reached the RPC
    result = servicer.received_command_results[0]
    assert result.success is False
    assert "unsupported inbound protocol" in result.error_message


@pytest.mark.asyncio
async def test_enrollment_rejected_raises():
    servicer = MockAgentControlServicer(accept=False, reject_reason="token expired")

    async with running(servicer) as address:
        client = ControlClient(make_settings(address), docker_manager=_FakeDockerManager())
        with pytest.raises(EnrollmentRejected):
            await asyncio.wait_for(client.run_session(), timeout=5)


# --- Identity: CSR at bootstrap, persisted-identity reconnect ---------------------
#
# See identity.py and control_client.py's run_session docstring for the full
# design. These tests use MockAgentControlServicer's real CSR-signing (see
# mock_platform_server.py) -- a real EC/P-256 keypair+CSR is generated by
# identity.py, a real (test-only, throwaway-CA-signed) X.509 certificate
# comes back, and this Agent's own real persist/reconnect code is what's
# under test. What is NOT verified here (see the final report for this
# slice): interop with the *real* Platform CA -- that's a sibling session's
# independent implementation, not visible to or exercised by this sandboxed
# test.


@pytest.mark.asyncio
async def test_bootstrap_enrollment_persists_real_csr_signed_identity(tmp_path):
    state_dir = tmp_path / "agent-state"
    servicer = MockAgentControlServicer(node_id="node-persisted-1", accept=True)

    async with running(servicer) as address:
        client = ControlClient(
            make_settings(address, state_dir=state_dir), docker_manager=_FakeDockerManager()
        )
        session_task = asyncio.create_task(client.run_session())
        try:
            await asyncio.wait_for(servicer.heartbeat_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task

    # A real CSR reached the mock server and was signed -- not the old
    # empty/placeholder Enrollment.csr_pem from the previous sub-slice.
    assert len(servicer.received_enrollments) == 1
    csr_pem = servicer.received_enrollments[0].csr_pem
    assert csr_pem.startswith(b"-----BEGIN CERTIFICATE REQUEST-----")

    key_path = state_dir / "client.key.pem"
    cert_path = state_dir / "client.cert.pem"
    node_id_path = state_dir / "node_id"
    assert key_path.is_file()
    assert cert_path.is_file()
    assert node_id_path.is_file()
    assert node_id_path.read_text(encoding="utf-8").strip() == "node-persisted-1"
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert cert_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")

    # The persisted certificate is a real X.509 cert for the real keypair
    # identity.py generated -- not a copy of the mock's placeholder bytes.
    from cryptography import x509  # noqa: PLC0415 - test-local, avoids a module-level dependency just for one assertion
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert cert.public_key().public_numbers() == private_key.public_key().public_numbers()


@pytest.mark.asyncio
async def test_persist_failure_after_successful_enrollment_does_not_crash_session(tmp_path):
    """A local disk-write failure persisting the issued identity must not
    take down an otherwise-successful session -- Platform already accepted
    the enrollment (the one-time token is already spent) by the time this
    can fail; the only real cost is losing the reconnect-without-token
    benefit on the *next* restart, not this session's own function. See
    control_client.py's enrollment_ack handling and agent/README.md's fresh
    TODO on this (found while building this slice, together with the
    matching Dockerfile fix for state_dir ownership).
    """
    state_dir = tmp_path / "blocked-state-dir"
    state_dir.write_text("a plain file occupies this path, so mkdir() inside "
                          "identity.persist_identity must fail with OSError")

    servicer = MockAgentControlServicer(node_id="node-persist-fail", accept=True)

    async with running(servicer) as address:
        client = ControlClient(
            make_settings(address, state_dir=state_dir), docker_manager=_FakeDockerManager()
        )
        session_task = asyncio.create_task(client.run_session())
        try:
            # The session must still reach a normal heartbeat despite the
            # persist failure -- if it crashed instead, this would time out.
            await asyncio.wait_for(servicer.heartbeat_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task

    assert client.node_id == "node-persist-fail"
    assert servicer.received_heartbeats[0].node_id == "node-persist-fail"
    # Nothing got persisted -- the blocking file is still just a file.
    assert state_dir.is_file()


@pytest.mark.asyncio
async def test_second_run_session_reuses_persisted_identity_without_reenrolling(tmp_path):
    state_dir = tmp_path / "agent-state"

    # Session 1: fresh node, no persisted identity yet -- bootstrap-enrolls,
    # persists the issued identity (same real flow as the test above).
    bootstrap_servicer = MockAgentControlServicer(node_id="node-persisted-2", accept=True)
    async with running(bootstrap_servicer) as address:
        client = ControlClient(
            make_settings(address, state_dir=state_dir), docker_manager=_FakeDockerManager()
        )
        session_task = asyncio.create_task(client.run_session())
        try:
            await asyncio.wait_for(bootstrap_servicer.heartbeat_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task
    assert len(bootstrap_servicer.received_enrollments) == 1

    persisted_key_bytes = (state_dir / "client.key.pem").read_bytes()
    persisted_cert_bytes = (state_dir / "client.cert.pem").read_bytes()

    # Session 2: simulates a fresh process start against a brand new mock
    # server instance (a real restart, not just a reconnect within the same
    # in-memory ControlClient) -- a NEW ControlClient reading the SAME
    # state_dir on disk, exactly like a real Agent restart would.
    reconnect_command = pb2.Command(
        command_id="cmd-reconnect-1", remove_outbound=pb2.RemoveOutbound(tag="whatever")
    )
    reconnect_servicer = MockAgentControlServicer(
        node_id="node-persisted-2", accept=True, send_command=reconnect_command
    )
    async with running(reconnect_servicer) as address:
        client2 = ControlClient(
            make_settings(address, state_dir=state_dir), docker_manager=_FakeDockerManager()
        )
        session_task = asyncio.create_task(client2.run_session())
        try:
            await asyncio.wait_for(reconnect_servicer.command_result_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task

    # The defining behavior (see run_session docstring): no Enrollment sent
    # at all this session.
    assert reconnect_servicer.received_enrollments == []
    # node_id was known immediately (from the persisted file), not learned
    # from an EnrollmentAck this session.
    assert client2.node_id == "node-persisted-2"
    # The very first message on the wire was a Heartbeat carrying that
    # node_id -- the reconnect signal itself.
    assert len(reconnect_servicer.received_heartbeats) >= 1
    assert reconnect_servicer.received_heartbeats[0].node_id == "node-persisted-2"
    # A Command delivered on that reconnect session still round-trips
    # normally (mock_platform_server.py's heartbeat-triggered send_command
    # path, see that module) -- persisted-identity reconnect doesn't break
    # ordinary command dispatch.
    assert len(reconnect_servicer.received_command_results) == 1
    assert reconnect_servicer.received_command_results[0].command_id == "cmd-reconnect-1"

    # Identity on disk is untouched by the reconnect (no re-persistence,
    # since no new EnrollmentAck was ever received this session).
    assert (state_dir / "client.key.pem").read_bytes() == persisted_key_bytes
    assert (state_dir / "client.cert.pem").read_bytes() == persisted_cert_bytes


@pytest.mark.asyncio
async def test_build_channel_prefers_persisted_identity_over_configured_paths(tmp_path, monkeypatch):
    """Focused unit test of the credential-selection decision in
    ControlClient._build_channel: when a persisted identity is present, its
    cert/key bytes are what get handed to grpc.ssl_channel_credentials --
    NOT settings.mtls_client_cert_path/mtls_client_key_path, even if both
    are also configured. grpc.ssl_channel_credentials() returns an opaque
    object with no introspectable getters, so this monkeypatches it to
    capture what it was called with -- the only way to observe this
    decision without a full real TLS handshake (covered at the protocol
    level, not the credential-selection level, by the insecure-mode
    end-to-end tests elsewhere in this file).
    """
    import dist_vpn_agent.control_client as control_client_module
    from dist_vpn_agent import identity

    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(b"fake-ca-bytes")
    configured_cert_path = tmp_path / "configured-client.pem"
    configured_cert_path.write_bytes(b"CONFIGURED-CERT-should-not-be-used")
    configured_key_path = tmp_path / "configured-client.key"
    configured_key_path.write_bytes(b"CONFIGURED-KEY-should-not-be-used")

    persisted_state_dir = tmp_path / "state"
    persisted = identity.persist_identity(
        persisted_state_dir,
        node_id="node-cred-test",
        private_key_pem=b"PERSISTED-KEY-should-be-used",
        cert_pem=b"PERSISTED-CERT-should-be-used",
    )

    captured_kwargs: dict = {}

    def _fake_ssl_channel_credentials(**kwargs):
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(control_client_module.grpc, "ssl_channel_credentials", _fake_ssl_channel_credentials)
    # secure_channel is also called with the (fake) credentials object above
    # -- stub it too so this test never actually tries to open a socket.
    monkeypatch.setattr(control_client_module.grpc.aio, "secure_channel", lambda *a, **k: object())

    settings = make_settings(
        "unused:50051",
        insecure_dev_mode=False,
        mtls_ca_cert_path=ca_path,
        mtls_client_cert_path=configured_cert_path,
        mtls_client_key_path=configured_key_path,
    )
    client = ControlClient(settings, docker_manager=_FakeDockerManager())

    client._build_channel(persisted)

    assert captured_kwargs["private_key"] == b"PERSISTED-KEY-should-be-used"
    assert captured_kwargs["certificate_chain"] == b"PERSISTED-CERT-should-be-used"
    # CA path is orthogonal to the persisted-vs-configured client identity
    # choice -- always read from settings regardless.
    assert captured_kwargs["root_certificates"] == b"fake-ca-bytes"

    # And the inverse: with no persisted identity, the configured paths win.
    captured_kwargs.clear()
    client._build_channel(None)
    assert captured_kwargs["private_key"] == b"CONFIGURED-KEY-should-not-be-used"
    assert captured_kwargs["certificate_chain"] == b"CONFIGURED-CERT-should-not-be-used"

    assert client.node_id is None
