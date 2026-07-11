"""A minimal in-process AgentControl servicer for exercising ControlClient
end-to-end without a real Platform. Test-only code.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import grpc.aio

from dist_vpn_agent.generated import agent_control_pb2 as pb2
from dist_vpn_agent.generated import agent_control_pb2_grpc as pb2_grpc


class MockAgentControlServicer(pb2_grpc.AgentControlServicer):
    def __init__(
        self,
        node_id: str = "node-test",
        accept: bool = True,
        reject_reason: str = "",
        send_command: pb2.Command | None = None,
    ) -> None:
        self.node_id = node_id
        self.accept = accept
        self.reject_reason = reject_reason
        self.send_command = send_command

        self.received_enrollments: list[pb2.Enrollment] = []
        self.received_heartbeats: list[pb2.Heartbeat] = []
        self.received_command_results: list[pb2.CommandResult] = []

        self.heartbeat_received = asyncio.Event()
        self.command_result_received = asyncio.Event()

    async def Session(self, request_iterator, context):  # noqa: N802 - gRPC-generated name
        async for message in request_iterator:
            kind = message.WhichOneof("payload")

            if kind == "enrollment":
                self.received_enrollments.append(message.enrollment)
                yield pb2.PlatformMessage(
                    enrollment_ack=pb2.EnrollmentAck(
                        node_id=self.node_id,
                        accepted=self.accept,
                        reason=self.reject_reason,
                        mtls_client_cert_pem=b"fake-cert-pem" if self.accept else b"",
                    )
                )
                if self.accept:
                    yield pb2.PlatformMessage(ping=pb2.Ping(timestamp_unix=1))
                    if self.send_command is not None:
                        yield pb2.PlatformMessage(command=self.send_command)

            elif kind == "heartbeat":
                self.received_heartbeats.append(message.heartbeat)
                self.heartbeat_received.set()

            elif kind == "command_result":
                self.received_command_results.append(message.command_result)
                self.command_result_received.set()

            # telemetry / test_result: not used by this slice's tests.


@contextlib.asynccontextmanager
async def running(servicer: MockAgentControlServicer) -> AsyncIterator[str]:
    """Start `servicer` on an ephemeral loopback port; yields "host:port"."""
    server = grpc.aio.server()
    pb2_grpc.add_AgentControlServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        with contextlib.suppress(Exception):
            await server.stop(None)
