"""Focused unit tests for identity.py, independent of any gRPC session --
see tests/test_control_client.py's "Identity: CSR at bootstrap, persisted-
identity reconnect" section for the end-to-end (real CSR sent, real mock-CA
signed cert persisted, real second-session reconnect) coverage of how this
module is actually wired into control_client.py.
"""

from __future__ import annotations

import stat
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from dist_vpn_agent.identity import (
    _CERT_MODE,
    _PRIVATE_KEY_MODE,
    generate_csr,
    load_persisted_identity,
    persist_identity,
)


def test_generate_csr_produces_a_real_ec_p256_self_signed_csr():
    generated = generate_csr(common_name="node-abc.example")

    csr = x509.load_pem_x509_csr(generated.csr_pem)
    # Real proof-of-possession check -- the CSR is signed by the private key
    # whose public half it carries, exactly what a real CA must verify
    # before it will issue a certificate for that key.
    assert csr.is_signature_valid

    public_key = csr.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert public_key.curve.name == "secp256r1"  # P-256

    (cn_attr,) = csr.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    assert cn_attr.value == "node-abc.example"

    # The private key PEM is real, loadable, and is actually the matching
    # private half of the CSR's public key -- not just two independently
    # generated, unrelated blobs.
    private_key = serialization.load_pem_private_key(generated.private_key_pem, password=None)
    assert isinstance(private_key, ec.EllipticCurvePrivateKey)
    assert private_key.public_key().public_numbers() == public_key.public_numbers()


def test_generate_csr_is_fresh_every_call():
    first = generate_csr(common_name="same-hostname")
    second = generate_csr(common_name="same-hostname")
    # Two independently generated EC/P-256 keypairs practically never
    # collide -- this is really asserting "generate_csr doesn't cache/reuse
    # a keypair across calls", which control_client.py's "fresh keypair per
    # bootstrap attempt" design (see its _build_enrollment_message
    # docstring) depends on.
    assert first.private_key_pem != second.private_key_pem
    assert first.csr_pem != second.csr_pem


def test_load_persisted_identity_returns_none_when_state_dir_is_empty(tmp_path):
    assert load_persisted_identity(tmp_path / "does-not-exist-yet") is None
    tmp_path.mkdir(exist_ok=True)
    assert load_persisted_identity(tmp_path) is None


@pytest.mark.parametrize("missing_file", ["client.key.pem", "client.cert.pem", "node_id"])
def test_load_persisted_identity_is_all_or_nothing(tmp_path, missing_file):
    # persist a complete identity, then delete exactly one file -- an
    # incomplete identity must be treated the same as "nothing persisted"
    # (safe fallback: bootstrap-enroll again), never raise or return a
    # partially-populated result.
    persist_identity(
        tmp_path, node_id="node-partial", private_key_pem=b"key-bytes", cert_pem=b"cert-bytes"
    )
    (tmp_path / missing_file).unlink()

    assert load_persisted_identity(tmp_path) is None


def test_load_persisted_identity_with_empty_node_id_file_is_treated_as_absent(tmp_path):
    persist_identity(tmp_path, node_id="node-x", private_key_pem=b"key-bytes", cert_pem=b"cert-bytes")
    (tmp_path / "node_id").write_text("   \n", encoding="utf-8")  # whitespace-only

    assert load_persisted_identity(tmp_path) is None


def test_persist_identity_round_trips_content_and_creates_state_dir(tmp_path):
    state_dir = tmp_path / "nested" / "state"  # does not exist yet
    persisted = persist_identity(
        state_dir, node_id="node-round-trip", private_key_pem=b"the-private-key", cert_pem=b"the-cert"
    )

    assert persisted.node_id == "node-round-trip"
    assert persisted.key_path == state_dir / "client.key.pem"
    assert persisted.cert_path == state_dir / "client.cert.pem"
    assert persisted.key_path.read_bytes() == b"the-private-key"
    assert persisted.cert_path.read_bytes() == b"the-cert"
    assert (state_dir / "node_id").read_text(encoding="utf-8") == "node-round-trip"

    loaded = load_persisted_identity(state_dir)
    assert loaded == persisted


def test_persist_identity_overwrites_previous_identity(tmp_path):
    persist_identity(tmp_path, node_id="node-old", private_key_pem=b"old-key", cert_pem=b"old-cert")
    persist_identity(tmp_path, node_id="node-new", private_key_pem=b"new-key", cert_pem=b"new-cert")

    loaded = load_persisted_identity(tmp_path)
    assert loaded.node_id == "node-new"
    assert (tmp_path / "client.key.pem").read_bytes() == b"new-key"
    assert (tmp_path / "client.cert.pem").read_bytes() == b"new-cert"


@pytest.mark.skipif(sys.platform == "win32", reason="Path.chmod is a no-op on Windows -- see identity.py module docstring's chmod note")
def test_persist_identity_sets_real_unix_permissions(tmp_path):
    persisted = persist_identity(
        tmp_path, node_id="node-perm", private_key_pem=b"key-bytes", cert_pem=b"cert-bytes"
    )

    key_mode = stat.S_IMODE(persisted.key_path.stat().st_mode)
    cert_mode = stat.S_IMODE(persisted.cert_path.stat().st_mode)
    assert key_mode == _PRIVATE_KEY_MODE
    assert cert_mode == _CERT_MODE
