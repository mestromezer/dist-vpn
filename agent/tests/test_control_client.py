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
    defaults = dict(
        platform_address=address,
        enrollment_token="one-time-token-123",
        hostname="test-host",
        agent_version="0.1.0-test",
        requested_role=NodeRole.ENTRY,
        heartbeat_interval_seconds=0.05,
        insecure_dev_mode=True,
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


@pytest.mark.asyncio
async def test_run_test_command_returns_not_implemented_result():
    # RunTest is one of the two ops this slice deliberately leaves
    # unimplemented (see agent/README.md) -- unlike the nine real ops
    # dispatched elsewhere, it should still get an honest, explicit
    # CommandResult(success=False), never a silent hang or a crash of the
    # whole session.
    command = pb2.Command(command_id="cmd-1", run_test=pb2.RunTest(test_id="t1", probe_url="https://example.com"))
    servicer = MockAgentControlServicer(node_id="node-xyz", accept=True, send_command=command)

    async with running(servicer) as address:
        client = ControlClient(make_settings(address), docker_manager=_FakeDockerManager())
        session_task = asyncio.create_task(client.run_session())
        try:
            await asyncio.wait_for(servicer.command_result_received.wait(), timeout=5)
        finally:
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task

    assert len(servicer.received_command_results) == 1
    result = servicer.received_command_results[0]
    assert result.command_id == "cmd-1"
    assert result.success is False
    assert "not implemented in this slice" in result.error_message


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

    assert client.node_id is None
