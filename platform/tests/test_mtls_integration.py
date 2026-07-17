"""Real mTLS gRPC integration test (TZ-5 §6.2).

Unlike the other grpc tests (test_grpc_servicer.py, test_grpc_dispatch.py,
tests/grpc_test_helpers.py::FakeAgent), which drive `AgentControlServicer.Session()`
directly with fake in-memory request iterators to keep the DB-facing logic tests fast
and transport-agnostic, this file opens *actual* `grpc.aio.Server`s bound to real
loopback sockets and connects to them with real `grpc.aio` secure channels - proving
the mTLS wiring itself (not just the CSR-signing math in test_mtls_ca.py) actually
works end-to-end.

grpc_server/server.py runs TWO listeners (see its module docstring for the full "why",
including the require_client_auth=False dead end this replaced - grpc's C-core turned
out not to expose an *offered* client cert to the application at all once client auth
isn't required for a listener, which defeated application-layer verification):

  - The strict listener (`require_client_auth=True`, `AgentControlServicer(
    enrollment_only=False)`): unchanged mTLS semantics from the original design.
      1. A client presenting a cert signed by the server's own CA can open the channel
         and interact freely (Heartbeat included, once already enrolled).
      2. A client presenting NO certificate at all cannot even establish the channel.
      3. A client presenting a certificate signed by a *different*, untrusted CA
         cannot establish the channel either - `require_client_auth=True` really is
         chain-verified by grpc/BoringSSL itself, unlike the False case.
  - The enrollment-only listener (`require_client_auth=False`, `AgentControlServicer(
    enrollment_only=True)`): no client cert requested or checked at all.
      4. A client presenting no cert can open the channel and complete a real
         Enrollment round trip.
      5. That same kind of connection sending anything other than Enrollment gets no
         reply - Session() refuses to process it, regardless of what any client claims
         to be, because this listener has no cert-based trust to check in the first
         place.
"""
from __future__ import annotations

import asyncio

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.generated import agent_control_pb2_grpc as pb_grpc
from dist_vpn_platform.grpc_server.servicer import AgentControlServicer
from dist_vpn_platform.security import mtls_ca
from tests.grpc_test_helpers import generate_test_csr_pem, seed_enrollment_token

_CHANNEL_READY_TIMEOUT = 5.0


def _server_credentials(material: mtls_ca.MTLSMaterial, *, require_client_auth: bool) -> grpc.ServerCredentials:
    return grpc.ssl_server_credentials(
        [(material.server.key_pem, material.server.cert_pem)],
        root_certificates=material.ca.cert_pem if require_client_auth else None,
        require_client_auth=require_client_auth,
    )


async def _start_strict_server(ca_dir) -> tuple[grpc.aio.Server, int, mtls_ca.MTLSMaterial]:
    material = mtls_ca.get_mtls_material(ca_dir)
    server = grpc.aio.server()
    pb_grpc.add_AgentControlServicer_to_server(AgentControlServicer(enrollment_only=False), server)
    port = server.add_secure_port("127.0.0.1:0", _server_credentials(material, require_client_auth=True))
    await server.start()
    return server, port, material


async def _start_enrollment_server(ca_dir) -> tuple[grpc.aio.Server, int, mtls_ca.MTLSMaterial]:
    material = mtls_ca.get_mtls_material(ca_dir)
    server = grpc.aio.server()
    pb_grpc.add_AgentControlServicer_to_server(AgentControlServicer(enrollment_only=True), server)
    port = server.add_secure_port("127.0.0.1:0", _server_credentials(material, require_client_auth=False))
    await server.start()
    return server, port, material


def _build_client_key_and_csr(common_name: str) -> tuple[bytes, bytes]:
    """Stands in for what an Agent does locally at bootstrap (TZ-4 §5.2 / proto
    comment on Enrollment.csr_pem): generate its own keypair, the private key never
    leaves this function/process, only the CSR (containing the public key) does."""
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key_pem, csr.public_bytes(serialization.Encoding.PEM)


@pytest.mark.asyncio
async def test_certified_client_can_interact_on_the_strict_listener(tmp_path, db_engine: None) -> None:
    server, port, material = await _start_strict_server(tmp_path / "ca")
    try:
        # A cert signed directly against the real CA, simulating an already-issued
        # identity (e.g. a reconnect after enrollment happened elsewhere, on the
        # enrollment-only listener - see the next test file section down).
        client_key_pem, client_csr_pem = _build_client_key_and_csr("integration-test-agent")
        client_cert_pem = mtls_ca.sign_client_csr(material.ca, client_csr_pem)

        channel_credentials = grpc.ssl_channel_credentials(
            root_certificates=material.ca.cert_pem,
            private_key=client_key_pem,
            certificate_chain=client_cert_pem,
        )
        channel = grpc.aio.secure_channel(f"localhost:{port}", channel_credentials)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=_CHANNEL_READY_TIMEOUT)

            stub = pb_grpc.AgentControlStub(channel)

            async def _requests():
                yield pb.AgentMessage(heartbeat=pb.Heartbeat(node_id="integration-test-node"))

            call = stub.Session(_requests())
            # The strict listener never restricts message kinds (its transport layer
            # already guarantees a verified cert on anything reaching it at all) - a
            # Heartbeat over a real mTLS channel should just be accepted, not refused.
            # There's no reply to a Heartbeat specifically (see servicer.py), so the
            # meaningful assertion is that the call stays open rather than closing
            # immediately the way the uncertified/untrusted cases below do.
            await asyncio.sleep(0.2)
            assert not call.done()
            call.cancel()
        finally:
            await channel.close()
    finally:
        await server.stop(None)


@pytest.mark.asyncio
async def test_strict_listener_rejects_client_with_no_certificate(tmp_path) -> None:
    server, port, _material = await _start_strict_server(tmp_path / "ca")
    try:
        insecure_channel = grpc.aio.insecure_channel(f"localhost:{port}")
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(insecure_channel.channel_ready(), timeout=_CHANNEL_READY_TIMEOUT)
        finally:
            await insecure_channel.close()
    finally:
        await server.stop(None)


@pytest.mark.asyncio
async def test_strict_listener_rejects_client_certificate_from_untrusted_ca(tmp_path) -> None:
    server, port, material = await _start_strict_server(tmp_path / "ca")
    try:
        # A second, wholly unrelated CA - simulates a rogue/never-enrolled client
        # trying to present a certificate the Platform's real CA never signed.
        rogue_material = mtls_ca.get_mtls_material(tmp_path / "rogue-ca")
        client_key_pem, client_csr_pem = _build_client_key_and_csr("rogue-agent")
        rogue_client_cert_pem = mtls_ca.sign_client_csr(rogue_material.ca, client_csr_pem)

        # Trusts the REAL server's CA to verify the server side (so a failure here
        # can't be mistaken for "server cert not trusted") but presents a client cert
        # signed by the rogue CA - require_client_auth=True + the server's real CA
        # root must reject it at the transport layer.
        channel_credentials = grpc.ssl_channel_credentials(
            root_certificates=material.ca.cert_pem,
            private_key=client_key_pem,
            certificate_chain=rogue_client_cert_pem,
        )
        channel = grpc.aio.secure_channel(f"localhost:{port}", channel_credentials)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(channel.channel_ready(), timeout=_CHANNEL_READY_TIMEOUT)
        finally:
            await channel.close()
    finally:
        await server.stop(None)


@pytest.mark.asyncio
async def test_enrollment_listener_completes_enrollment_with_no_client_certificate(
    tmp_path, db_engine: None
) -> None:
    server, port, material = await _start_enrollment_server(tmp_path / "ca")
    try:
        # No client cert offered at all - the enrollment-only listener never asks for
        # one (require_client_auth=False, no root_certificates - see server.py). This
        # is still a real TLS channel (verifies the SERVER's cert), not plaintext.
        no_cert_credentials = grpc.ssl_channel_credentials(root_certificates=material.ca.cert_pem)
        channel = grpc.aio.secure_channel(f"localhost:{port}", no_cert_credentials)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=_CHANNEL_READY_TIMEOUT)

            stub = pb_grpc.AgentControlStub(channel)
            plaintext = await seed_enrollment_token()

            async def _enroll_request():
                yield pb.AgentMessage(
                    enrollment=pb.Enrollment(
                        enrollment_token=plaintext,
                        hostname="no-cert-agent",
                        requested_role=pb.NODE_ROLE_ENTRY,
                        csr_pem=generate_test_csr_pem("no-cert-agent"),
                    )
                )

            call = stub.Session(_enroll_request())
            reply = await call.read()
            assert reply.WhichOneof("payload") == "enrollment_ack"
            assert reply.enrollment_ack.accepted is True
            call.cancel()
        finally:
            await channel.close()
    finally:
        await server.stop(None)


@pytest.mark.asyncio
async def test_enrollment_listener_refuses_anything_other_than_enrollment(tmp_path) -> None:
    server, port, material = await _start_enrollment_server(tmp_path / "ca")
    try:
        no_cert_credentials = grpc.ssl_channel_credentials(root_certificates=material.ca.cert_pem)
        channel = grpc.aio.secure_channel(f"localhost:{port}", no_cert_credentials)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=_CHANNEL_READY_TIMEOUT)
            stub = pb_grpc.AgentControlStub(channel)

            async def _heartbeat_request():
                yield pb.AgentMessage(heartbeat=pb.Heartbeat(node_id="some-node-id"))

            call = stub.Session(_heartbeat_request())
            reply = await call.read()
            assert reply == grpc.aio.EOF, "the enrollment-only listener must not process a Heartbeat"
        finally:
            await channel.close()
    finally:
        await server.stop(None)
