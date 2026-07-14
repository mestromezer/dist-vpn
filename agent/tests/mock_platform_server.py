"""A minimal in-process AgentControl servicer for exercising ControlClient
end-to-end without a real Platform. Test-only code.

--- CSR signing (added this sub-slice, see agent/README.md and identity.py)
    ---

``control_client.py`` always sends a real PKCS#10 CSR in
``Enrollment.csr_pem`` now (identity.py's ``generate_csr``). This servicer
signs it with a throwaway, module-generated test CA (``_generate_test_ca``,
real ``cryptography`` X.509, not a byte-string placeholder) and returns the
signed certificate PEM as ``EnrollmentAck.mtls_client_cert_pem`` -- close
enough to a real CA's shape (subject copied from the CSR, real EC/P-256
signature, bounded validity) to exercise this Agent's real
persist/reconnect code against a real certificate, while being explicit that
this is *not* the Platform's actual CA/signing implementation (a sibling
session's independent work, not visible here -- see agent/README.md's "what
is/isn't real-verified" notes for exactly what this proves and doesn't).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
from collections.abc import AsyncIterator

import grpc.aio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from dist_vpn_agent.generated import agent_control_pb2 as pb2
from dist_vpn_agent.generated import agent_control_pb2_grpc as pb2_grpc

# Fallback cert bytes for the (not exercised by control_client.py anymore,
# but still a legal AgentMessage to construct) case of an Enrollment with no
# CSR at all -- kept so this mock doesn't need real X.509 machinery for that
# degenerate case.
_PLACEHOLDER_CERT_PEM = b"fake-cert-pem"

# Test-only cert validity. 90 days is the sibling Platform-track session's
# stated choice for the real CA (see this slice's task context) -- mirrored
# here for realism, but this mock's validity period is NOT a claim about
# what the real Platform CA will actually do; only that track's own code is
# authoritative for that.
_MOCK_CERT_VALIDITY_DAYS = 90


def _generate_test_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mock-platform-test-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _sign_csr(csr_pem: bytes, ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate) -> bytes:
    """Load the real CSR the Agent sent, verify its self-signature (proof of
    possession of the private key -- what a real CA must also check before
    trusting the public key it's about to certify), and issue a certificate
    for it signed by this mock's throwaway CA.
    """
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise ValueError("CSR signature is invalid -- Agent did not prove possession of the private key")

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_MOCK_CERT_VALIDITY_DAYS))
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


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
        self._ca_key, self._ca_cert = _generate_test_ca()

        self.received_enrollments: list[pb2.Enrollment] = []
        self.received_heartbeats: list[pb2.Heartbeat] = []
        self.received_command_results: list[pb2.CommandResult] = []
        self.received_test_results: list[pb2.TestResult] = []

        self.heartbeat_received = asyncio.Event()
        self.command_result_received = asyncio.Event()
        self.test_result_received = asyncio.Event()

    async def Session(self, request_iterator, context):  # noqa: N802 - gRPC-generated name
        # Local to *this* Session() call/generator instance -- distinct from
        # self.received_enrollments, which accumulates across every session
        # this servicer instance has ever handled (a servicer commonly
        # outlives one session in reconnect tests, see "running" below and
        # test_control_client.py's persisted-identity tests). Needed so the
        # "was an Enrollment sent in *this* session" check in the heartbeat
        # branch below is correct even when an *earlier* session on the same
        # servicer did send one.
        enrolled_this_session = False

        async for message in request_iterator:
            kind = message.WhichOneof("payload")

            if kind == "enrollment":
                enrolled_this_session = True
                self.received_enrollments.append(message.enrollment)
                if self.accept:
                    if message.enrollment.csr_pem:
                        cert_pem = _sign_csr(message.enrollment.csr_pem, self._ca_key, self._ca_cert)
                    else:
                        cert_pem = _PLACEHOLDER_CERT_PEM
                else:
                    cert_pem = b""
                yield pb2.PlatformMessage(
                    enrollment_ack=pb2.EnrollmentAck(
                        node_id=self.node_id,
                        accepted=self.accept,
                        reason=self.reject_reason,
                        mtls_client_cert_pem=cert_pem,
                    )
                )
                if self.accept:
                    yield pb2.PlatformMessage(ping=pb2.Ping(timestamp_unix=1))
                    if self.send_command is not None:
                        yield pb2.PlatformMessage(command=self.send_command)

            elif kind == "heartbeat":
                self.received_heartbeats.append(message.heartbeat)
                self.heartbeat_received.set()
                if not enrolled_this_session and self.send_command is not None:
                    # Heartbeat-first reconnect (persisted identity, see
                    # control_client.py's run_session docstring): this
                    # session never sent an Enrollment, so the branch above
                    # never ran. Still deliver a queued test Command on the
                    # first Heartbeat so reconnect-path tests can exercise
                    # command dispatch too, not just enrollment-path ones.
                    yield pb2.PlatformMessage(command=self.send_command)
                    self.send_command = None  # once per session, same as the enrollment branch

            elif kind == "command_result":
                self.received_command_results.append(message.command_result)
                self.command_result_received.set()

            elif kind == "test_result":
                self.received_test_results.append(message.test_result)
                self.test_result_received.set()

            # telemetry: not used by this slice's tests.


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
