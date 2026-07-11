"""End-to-end test: ControlClient against an in-process mock AgentControl
server, covering enrollment -> heartbeat -> ping -> command round trip
(TZ-4 scope for this slice; see agent/README.md).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from dist_vpn_agent.config import AgentSettings, NodeRole
from dist_vpn_agent.control_client import ControlClient, EnrollmentRejected
from dist_vpn_agent.generated import agent_control_pb2 as pb2
from mock_platform_server import MockAgentControlServicer, running


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


@pytest.mark.asyncio
async def test_enrollment_heartbeat_ping_round_trip():
    servicer = MockAgentControlServicer(node_id="node-abc", accept=True)

    async with running(servicer) as address:
        client = ControlClient(make_settings(address))
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
async def test_command_round_trip_returns_not_implemented_result():
    command = pb2.Command(
        command_id="cmd-1",
        add_user=pb2.AddUser(inbound_tag="in1", uuid="u1", email="user@example.com"),
    )
    servicer = MockAgentControlServicer(node_id="node-xyz", accept=True, send_command=command)

    async with running(servicer) as address:
        client = ControlClient(make_settings(address))
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
    assert result.error_message == "not implemented in this slice"


@pytest.mark.asyncio
async def test_enrollment_rejected_raises():
    servicer = MockAgentControlServicer(accept=False, reject_reason="token expired")

    async with running(servicer) as address:
        client = ControlClient(make_settings(address))
        with pytest.raises(EnrollmentRejected):
            await asyncio.wait_for(client.run_session(), timeout=5)

    assert client.node_id is None
