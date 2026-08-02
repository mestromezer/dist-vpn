"""security/peering_identity.py + services/peering_client.py's construction-time
validation."""
from __future__ import annotations

import pytest
from cryptography import x509

from dist_vpn_platform.models.peer_platform import PeerPlatform
from dist_vpn_platform.security.peering_identity import generate_peering_identity
from dist_vpn_platform.services.peering_client import PeeringClient, PeeringNotConfiguredError


def test_generate_peering_identity_produces_a_valid_self_signed_cert() -> None:
    identity = generate_peering_identity(common_name="ru-platform")

    cert = x509.load_pem_x509_certificate(identity.cert_pem.encode("ascii"))
    assert cert.subject == cert.issuer  # self-signed
    common_name = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert common_name == "ru-platform"

    # Round-trips through PEM parsing cleanly - the smoke test that actually matters
    # for a value that gets pasted by an operator into a vault file (provisioning
    # slice 13), not just held in memory.
    from cryptography.hazmat.primitives import serialization

    serialization.load_pem_private_key(identity.key_pem.encode("ascii"), password=None)


def test_generate_peering_identity_is_fresh_each_call() -> None:
    first = generate_peering_identity(common_name="x")
    second = generate_peering_identity(common_name="x")
    assert first.key_pem != second.key_pem


def test_peering_client_requires_grpc_address() -> None:
    peer = PeerPlatform(
        id="peer-1",
        label="foreign-platform",
        local_peering_key_pem="k",
        local_peering_cert_pem="c",
        peer_cert_pem="p",
    )
    with pytest.raises(PeeringNotConfiguredError):
        PeeringClient(peer)


def test_peering_client_requires_certificate_material() -> None:
    peer = PeerPlatform(id="peer-1", label="foreign-platform", grpc_address="203.0.113.20:50053")
    with pytest.raises(PeeringNotConfiguredError):
        PeeringClient(peer)
