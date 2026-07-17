"""Real mTLS gRPC integration test (TZ-5 §6.2).

Unlike the other grpc tests (test_grpc_servicer.py, test_grpc_dispatch.py,
tests/grpc_test_helpers.py::FakeAgent), which drive `AgentControlServicer.Session()`
directly with fake in-memory request iterators to keep the DB-facing logic tests fast
and transport-agnostic, this file opens an *actual* `grpc.aio.Server` bound to a real
loopback socket with `ssl_server_credentials(require_client_auth=False)`, and connects
to it with real `grpc.aio` secure channels - proving the mTLS wiring itself (not just
the CSR-signing math in test_mtls_ca.py) actually works end-to-end:

  1. A client presenting a cert signed by the server's own CA can open the channel and
     complete a real Enrollment round trip.
  2. A client presenting NO certificate at all can still open the channel (this is
     required for real bootstrap enrollment to be possible at all - see
     grpc_server/server.py's module docstring for why `require_client_auth=True` made
     enrollment structurally impossible, found while validating a real two-VDS
     deployment) and can complete Enrollment, but cannot do anything else - a second,
     equally uncertified connection attempting a Heartbeat gets no reply, because
     grpc_server/servicer.py::Session refuses to process it.
  3. A client presenting a certificate signed by a *different*, untrusted CA also gets
     to open the channel (found while wiring up require_client_auth=False: grpc's
     C-core does NOT chain-verify an *offered* client cert in that mode at all - that's
     "request but don't verify", not "verify if offered"; only require_client_auth=True
     gets chain verification from grpc/BoringSSL itself) - but is refused the same way
     as the no-cert case the moment it tries anything, because Session() verifies the
     offered cert itself (security/mtls_ca.py::verify_client_cert_pem) rather than
     trusting grpc's word for it.
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


async def _start_real_server(ca_dir) -> tuple[grpc.aio.Server, int, mtls_ca.MTLSMaterial]:
    material = mtls_ca.get_mtls_material(ca_dir)
    server = grpc.aio.server()
    pb_grpc.add_AgentControlServicer_to_server(AgentControlServicer(), server)
    credentials = grpc.ssl_server_credentials(
        [(material.server.key_pem, material.server.cert_pem)],
        root_certificates=material.ca.cert_pem,
        require_client_auth=False,
    )
    port = server.add_secure_port("127.0.0.1:0", credentials)
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
async def test_real_mtls_channel_completes_enrollment(tmp_path, db_engine: None) -> None:
    server, port, material = await _start_real_server(tmp_path / "ca")
    try:
        # This channel's own client cert - signed directly against the real CA to
        # simulate an already-issued identity (e.g. a reconnect), just like the CSR
        # signed live during the Enrollment call inside the stream below.
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
            plaintext = await seed_enrollment_token()

            async def _requests():
                yield pb.AgentMessage(
                    enrollment=pb.Enrollment(
                        enrollment_token=plaintext,
                        hostname="integration-test-agent",
                        requested_role=pb.NODE_ROLE_ENTRY,
                        csr_pem=generate_test_csr_pem("integration-test-agent"),
                    )
                )

            call = stub.Session(_requests())
            reply = await call.read()
            assert reply.WhichOneof("payload") == "enrollment_ack"
            assert reply.enrollment_ack.accepted is True
            assert reply.enrollment_ack.mtls_client_cert_pem  # real signed cert, over the real mTLS channel
            call.cancel()
        finally:
            await channel.close()
    finally:
        await server.stop(None)


@pytest.mark.asyncio
async def test_client_with_no_certificate_can_enroll_but_nothing_else(tmp_path, db_engine: None) -> None:
    server, port, material = await _start_real_server(tmp_path / "ca")
    try:
        # A *TLS* channel (still verifies the server's own cert against the real CA -
        # this is not plaintext) that simply never offers a client certificate at all.
        # grpc.aio.insecure_channel would be the wrong tool here - that's plaintext, no
        # TLS handshake whatsoever, which could never reach a TLS-only server regardless
        # of require_client_auth and would prove nothing about this specific behavior.
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

        # A *second*, equally uncertified connection sending anything other than
        # Enrollment must be refused by servicer.py::Session, not processed - it
        # closes the stream immediately instead of yielding a reply.
        second_channel = grpc.aio.secure_channel(f"localhost:{port}", no_cert_credentials)
        try:
            await asyncio.wait_for(second_channel.channel_ready(), timeout=_CHANNEL_READY_TIMEOUT)
            stub = pb_grpc.AgentControlStub(second_channel)

            async def _heartbeat_request():
                yield pb.AgentMessage(heartbeat=pb.Heartbeat(node_id="some-node-id"))

            call = stub.Session(_heartbeat_request())
            reply = await call.read()
            assert reply == grpc.aio.EOF, "uncertified Heartbeat should get no reply, not be processed"
        finally:
            await second_channel.close()
    finally:
        await server.stop(None)


@pytest.mark.asyncio
async def test_client_certificate_from_untrusted_ca_is_rejected(tmp_path, db_engine: None) -> None:
    server, port, material = await _start_real_server(tmp_path / "ca")
    try:
        # A second, wholly unrelated CA - simulates a rogue/never-enrolled client
        # trying to present a certificate the Platform's real CA never signed.
        rogue_material = mtls_ca.get_mtls_material(tmp_path / "rogue-ca")
        client_key_pem, client_csr_pem = _build_client_key_and_csr("rogue-agent")
        rogue_client_cert_pem = mtls_ca.sign_client_csr(rogue_material.ca, client_csr_pem)

        # Trusts the REAL server's CA to verify the server side (so a failure here
        # can't be mistaken for "server cert not trusted") but presents a client cert
        # signed by the rogue CA. The channel itself opens fine - grpc's C-core doesn't
        # chain-verify an offered cert under require_client_auth=False (see module
        # docstring) - so the rejection has to be proven one level up, the same way as
        # the no-cert case: the rogue cert can't even complete Enrollment with it,
        # because Session()/verify_client_cert_pem() doesn't recognize it as ours
        # either way, and a rogue cert doesn't make Enrollment's own token check any
        # more likely to pass.
        channel_credentials = grpc.ssl_channel_credentials(
            root_certificates=material.ca.cert_pem,
            private_key=client_key_pem,
            certificate_chain=rogue_client_cert_pem,
        )
        channel = grpc.aio.secure_channel(f"localhost:{port}", channel_credentials)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=_CHANNEL_READY_TIMEOUT)
            stub = pb_grpc.AgentControlStub(channel)

            async def _heartbeat_request():
                yield pb.AgentMessage(heartbeat=pb.Heartbeat(node_id="some-node-id"))

            call = stub.Session(_heartbeat_request())
            reply = await call.read()
            assert reply == grpc.aio.EOF, "a cert from an untrusted CA must not be treated as authenticated"
        finally:
            await channel.close()
    finally:
        await server.stop(None)
