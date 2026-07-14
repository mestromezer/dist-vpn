"""Shared test helpers for driving grpc_server.servicer.AgentControlServicer.Session()
without a real gRPC channel - same "exercise the servicer directly, the transport
itself is grpcio-tools boilerplate" approach already used by test_grpc_servicer.py.

Two levels are provided:
  - FakeRequestIterator/FakeContext: the raw building blocks (used directly by
    test_grpc_dispatch.py, which needs to inspect the exact bidi-stream mechanics).
  - FakeAgent: a higher-level "well-behaved connected Agent" that enrolls and
    auto-acks every Command with success=True - used by REST-level tests
    (test_cascades.py, test_users.py) that just need a live node for dispatch_command()
    to actually reach, without re-asserting the low-level stream mechanics each time.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.grpc_server.servicer import AgentControlServicer
from dist_vpn_platform.models.enrollment_token import EnrollmentToken
from dist_vpn_platform.models.node import NodeRole
from dist_vpn_platform.security.enrollment_tokens import generate_token, hash_token


def generate_test_csr_pem(common_name: str = "fake-agent") -> bytes:
    """Build a real, self-signed PKCS#10 CSR (own throwaway EC keypair, never
    persisted) - Enrollment.csr_pem is mandatory as of the mTLS slice (security/
    mtls_ca.py::sign_client_csr rejects missing/malformed CSRs), so every test that
    drives enrollment (directly or via FakeAgent below) needs one that actually
    verifies, not a placeholder string."""
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)


class FakeContext:
    def peer(self) -> str:
        return "fake-peer"


class FakeRequestIterator:
    """Stands in for the real gRPC request_iterator (an Agent's outbound half of the
    bidi stream) - the test controls exactly what "arrives from the Agent" and when."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[pb.AgentMessage | None]" = asyncio.Queue()

    async def send(self, message: pb.AgentMessage) -> None:
        await self._queue.put(message)

    async def close(self) -> None:
        await self._queue.put(None)

    def __aiter__(self) -> "FakeRequestIterator":
        return self

    async def __anext__(self) -> pb.AgentMessage:
        message = await self._queue.get()
        if message is None:
            raise StopAsyncIteration
        return message


async def seed_enrollment_token(role: NodeRole = NodeRole.ENTRY) -> str:
    plaintext = generate_token()
    async with session_scope() as db:
        db.add(
            EnrollmentToken(
                token_hash=hash_token(plaintext),
                requested_role=role,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
        )
    return plaintext


class FakeAgent:
    """A simulated connected Agent: enrolls with a freshly-seeded token, then
    immediately acknowledges every Command it receives - close() ends its stream
    cleanly. `received_commands` records every Command seen, in order, for assertions.

    A `run_test` Command (Command.op == "run_test", dispatched via
    grpc_server/servicer.py::dispatch_test - see services/link_tests.py) gets a
    `TestResult` reply instead of a `CommandResult`, matching what a real Agent sends
    (contracts/proto/agent_control.proto: RunTest is answered by TestResult, correlated
    by test_id, not command_id/CommandResult) - the correlation mechanism itself
    (SessionRegistry.resolve()) is command_id-agnostic and resolves on either message
    kind, but a test harness that always replied with CommandResult would silently mask
    a real Agent behaving differently. `test_result_outcome`/`test_result_latency_ms`/
    `test_result_detail` are overridable per-test (see test_cascades.py) to exercise
    LinkTest outcomes other than the default success."""

    def __init__(
        self,
        *,
        test_result_outcome: "pb.TestOutcome.ValueType" = pb.TEST_OUTCOME_SUCCESS,
        test_result_latency_ms: int = 42,
        test_result_detail: str = "ok",
    ) -> None:
        self.requests = FakeRequestIterator()
        self.received_commands: list[pb.Command] = []
        self.node_id: str | None = None
        self._ready = asyncio.Event()
        self._test_result_outcome = test_result_outcome
        self._test_result_latency_ms = test_result_latency_ms
        self._test_result_detail = test_result_detail
        self._session = AgentControlServicer().Session(self.requests, FakeContext())
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        async for message in self._session:
            kind = message.WhichOneof("payload")
            if kind == "enrollment_ack":
                if message.enrollment_ack.accepted:
                    self.node_id = message.enrollment_ack.node_id
                self._ready.set()
            elif kind == "command":
                self.received_commands.append(message.command)
                if message.command.WhichOneof("op") == "run_test":
                    await self.requests.send(
                        pb.AgentMessage(
                            test_result=pb.TestResult(
                                test_id=message.command.run_test.test_id,
                                outcome=self._test_result_outcome,
                                latency_ms=self._test_result_latency_ms,
                                detail=self._test_result_detail,
                            )
                        )
                    )
                else:
                    await self.requests.send(
                        pb.AgentMessage(
                            command_result=pb.CommandResult(command_id=message.command.command_id, success=True)
                        )
                    )

    async def connect(self, role: NodeRole, *, hostname: str = "") -> str:
        plaintext = await seed_enrollment_token(role)
        await self.requests.send(
            pb.AgentMessage(
                enrollment=pb.Enrollment(
                    enrollment_token=plaintext,
                    hostname=hostname,
                    requested_role=pb.NODE_ROLE_ENTRY if role == NodeRole.ENTRY else pb.NODE_ROLE_EXIT,
                    csr_pem=generate_test_csr_pem(hostname or "fake-agent"),
                )
            )
        )
        await asyncio.wait_for(self._ready.wait(), timeout=5.0)
        assert self.node_id is not None, "enrollment was rejected by the fake agent test harness"
        return self.node_id

    async def close(self) -> None:
        await self.requests.close()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(self._pump_task, timeout=5.0)
