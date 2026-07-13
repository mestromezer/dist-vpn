"""Tests for the live per-node command-dispatch mechanism (grpc_server/servicer.py):
a `Command` sent via `dispatch_command()` is actually delivered onto that node's open
`Session()` bidi-stream, and a `CommandResult` sent back on that same stream resolves
the future `dispatch_command()` is awaiting - the mechanism the previous slice's
`dispatch_command` docstring described as deliberately not built yet (see git history
on this file for the old `raise NotImplementedError` stub).

This drives `Session()` directly (no real gRPC channel), same approach as
test_grpc_servicer.py - see grpc_test_helpers.py for the fake request_iterator/context.
"""
from __future__ import annotations

import asyncio

import pytest

from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.grpc_server.servicer import (
    AgentControlServicer,
    CommandDispatchTimeoutError,
    NodeNotConnectedError,
    dispatch_command,
    new_command_id,
)
from tests.grpc_test_helpers import (
    FakeContext,
    FakeRequestIterator,
    generate_test_csr_pem,
    seed_enrollment_token,
)


@pytest.mark.asyncio
async def test_dispatch_command_is_received_on_the_live_stream_and_result_resolves(db_engine: None) -> None:
    plaintext = await seed_enrollment_token()
    servicer = AgentControlServicer()
    fake_requests = FakeRequestIterator()
    session_gen = servicer.Session(fake_requests, FakeContext())

    # 1. The "Agent" enrolls - drive the handshake by hand to learn its node_id, which
    # is required before dispatch_command() has anywhere to send a Command.
    await fake_requests.send(
        pb.AgentMessage(
            enrollment=pb.Enrollment(
                enrollment_token=plaintext,
                requested_role=pb.NODE_ROLE_ENTRY,
                csr_pem=generate_test_csr_pem(),
            )
        )
    )
    enrollment_reply = await session_gen.__anext__()
    assert enrollment_reply.WhichOneof("payload") == "enrollment_ack"
    assert enrollment_reply.enrollment_ack.accepted is True
    node_id = enrollment_reply.enrollment_ack.node_id

    # 2. Call dispatch_command() concurrently with a task that drains whatever
    # Session() yields next (must be the dispatched Command) and echoes back a
    # CommandResult, simulating the Agent's side of the wire.
    command = pb.Command(command_id=new_command_id(), remove_inbound=pb.RemoveInbound(tag="hopa-in-test"))

    async def _drive_agent_side() -> None:
        message = await session_gen.__anext__()
        assert message.WhichOneof("payload") == "command"
        assert message.command.command_id == command.command_id
        assert message.command.WhichOneof("op") == "remove_inbound"
        assert message.command.remove_inbound.tag == "hopa-in-test"
        await fake_requests.send(
            pb.AgentMessage(command_result=pb.CommandResult(command_id=command.command_id, success=True))
        )

    driver_task = asyncio.create_task(_drive_agent_side())
    result = await dispatch_command(node_id, command, timeout=5.0)
    await driver_task

    assert result.success is True
    assert result.command_id == command.command_id

    # 3. Tear down cleanly - closing the request stream should end Session().
    await fake_requests.close()
    with pytest.raises(StopAsyncIteration):
        await session_gen.__anext__()


@pytest.mark.asyncio
async def test_dispatch_command_to_unconnected_node_raises_immediately(db_engine: None) -> None:
    with pytest.raises(NodeNotConnectedError):
        await dispatch_command("no-such-node", pb.Command(command_id=new_command_id()))


@pytest.mark.asyncio
async def test_dispatch_command_times_out_if_agent_never_replies(db_engine: None) -> None:
    plaintext = await seed_enrollment_token()
    servicer = AgentControlServicer()
    fake_requests = FakeRequestIterator()
    session_gen = servicer.Session(fake_requests, FakeContext())

    await fake_requests.send(
        pb.AgentMessage(
            enrollment=pb.Enrollment(
                enrollment_token=plaintext,
                requested_role=pb.NODE_ROLE_ENTRY,
                csr_pem=generate_test_csr_pem(),
            )
        )
    )
    enrollment_reply = await session_gen.__anext__()
    node_id = enrollment_reply.enrollment_ack.node_id

    async def _drain_one() -> None:
        # Receive the dispatched Command but never reply - simulates a connected-but-
        # unresponsive Agent.
        await session_gen.__anext__()

    drain_task = asyncio.create_task(_drain_one())
    with pytest.raises(CommandDispatchTimeoutError):
        await dispatch_command(node_id, pb.Command(command_id=new_command_id()), timeout=0.2)
    await drain_task

    await fake_requests.close()
