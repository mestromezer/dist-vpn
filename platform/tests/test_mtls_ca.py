"""Unit tests for security/mtls_ca.py (TZ-5 §6.2) - CA generation/persistence and
CSR-to-client-certificate signing, exercised directly against real `cryptography`
objects (no gRPC involved here - see test_mtls_integration.py for the real mTLS
channel-level test)."""
from __future__ import annotations

import base64

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from dist_vpn_platform.security import mtls_ca


def _verify_signed_by(cert: x509.Certificate, signer_public_key) -> bool:
    """True if `cert`'s signature verifies against `signer_public_key` - EC-only
    helper (both the CA and every cert it issues in this module use P-256/SHA-256,
    see security/mtls_ca.py's module docstring)."""
    try:
        signer_public_key.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
        return True
    except Exception:
        return False


def _der_to_pem(der: bytes, label: str) -> bytes:
    body = base64.encodebytes(der).decode("ascii")
    return f"-----BEGIN {label}-----\n{body}-----END {label}-----\n".encode("ascii")


def _build_test_csr_pem(common_name: str = "test-node") -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)


def test_ca_generation_creates_persistent_files(tmp_path) -> None:
    ca_dir = tmp_path / "ca"
    material = mtls_ca.get_mtls_material(ca_dir)

    assert (ca_dir / mtls_ca.CA_KEY_FILENAME).exists()
    assert (ca_dir / mtls_ca.CA_CERT_FILENAME).exists()
    assert (ca_dir / mtls_ca.SERVER_KEY_FILENAME).exists()
    assert (ca_dir / mtls_ca.SERVER_CERT_FILENAME).exists()
    assert material.ca.cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")


def test_ca_is_self_signed_root_with_ca_basic_constraint(tmp_path) -> None:
    material = mtls_ca.get_mtls_material(tmp_path / "ca")
    cert = material.ca.certificate

    assert cert.issuer == cert.subject  # self-signed
    basic_constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic_constraints.ca is True
    # Signed by its own private key - verifying against its own embedded public key
    # must succeed for a properly self-signed root.
    assert _verify_signed_by(cert, material.ca.private_key.public_key())


def test_ca_material_persists_across_simulated_restart(tmp_path) -> None:
    """A second call against the same ca_dir - after clearing the in-process cache,
    simulating a fresh process start - must load the exact same CA (not silently
    regenerate a new one, which would invalidate every already-issued Agent cert)."""
    ca_dir = tmp_path / "ca"
    first = mtls_ca.get_mtls_material(ca_dir)

    mtls_ca.reset_cache_for_tests()  # simulate the process restarting

    second = mtls_ca.get_mtls_material(ca_dir)

    assert first.ca.cert_pem == second.ca.cert_pem
    assert first.server.cert_pem == second.server.cert_pem
    assert first.server.key_pem == second.server.key_pem


def test_server_leaf_cert_chains_to_ca_and_covers_default_sans(tmp_path) -> None:
    material = mtls_ca.get_mtls_material(tmp_path / "ca")
    server_cert = x509.load_pem_x509_certificate(material.server.cert_pem)

    assert server_cert.issuer == material.ca.certificate.subject
    assert _verify_signed_by(server_cert, material.ca.private_key.public_key())

    san = server_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_addresses = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "127.0.0.1" in ip_addresses

    eku = server_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku


def test_server_leaf_cert_respects_custom_sans(tmp_path) -> None:
    material = mtls_ca.get_mtls_material(tmp_path / "ca", server_sans=["platform.internal", "10.0.0.5"])
    server_cert = x509.load_pem_x509_certificate(material.server.cert_pem)
    san = server_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

    assert "platform.internal" in san.get_values_for_type(x509.DNSName)
    assert "10.0.0.5" in [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]


def test_sign_client_csr_produces_cert_chaining_to_ca(tmp_path) -> None:
    material = mtls_ca.get_mtls_material(tmp_path / "ca")
    csr_pem = _build_test_csr_pem("agent-ru-01")

    cert_pem = mtls_ca.sign_client_csr(material.ca, csr_pem)
    cert = x509.load_pem_x509_certificate(cert_pem)

    assert cert.issuer == material.ca.certificate.subject
    assert cert.subject == x509.load_pem_x509_csr(csr_pem).subject
    assert _verify_signed_by(cert, material.ca.private_key.public_key())

    basic_constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic_constraints.ca is False
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku

    validity_days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
    assert validity_days == pytest.approx(mtls_ca.CLIENT_CERT_VALIDITY_DAYS, abs=1)


def test_sign_client_csr_rejects_missing_csr(tmp_path) -> None:
    material = mtls_ca.get_mtls_material(tmp_path / "ca")
    with pytest.raises(mtls_ca.CSRValidationError):
        mtls_ca.sign_client_csr(material.ca, b"")


def test_sign_client_csr_rejects_malformed_csr(tmp_path) -> None:
    material = mtls_ca.get_mtls_material(tmp_path / "ca")
    with pytest.raises(mtls_ca.CSRValidationError):
        mtls_ca.sign_client_csr(material.ca, b"this is not a PEM CSR at all")


def test_sign_client_csr_rejects_tampered_signature(tmp_path) -> None:
    """A syntactically well-formed CSR whose signature does not verify against its
    own embedded public key (e.g. corrupted in transit, or a buggy Agent-side CSR
    builder) must be rejected, not silently trusted."""
    material = mtls_ca.get_mtls_material(tmp_path / "ca")
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tampered")]))
        .sign(key, hashes.SHA256())
    )
    der = csr.public_bytes(serialization.Encoding.DER)
    # Flip the very last byte - the tail end of the signature BIT STRING's content
    # (the least-significant byte of the ECDSA `s` integer). This keeps every DER
    # length prefix identical (still parses cleanly) while invalidating the signature
    # value itself - unlike truncating/corrupting earlier bytes, which risks a parse
    # error instead of exercising the "parses fine but signature is wrong" path.
    tampered = der[:-1] + bytes([der[-1] ^ 0xFF])
    tampered_pem = _der_to_pem(tampered, "CERTIFICATE REQUEST")

    with pytest.raises(mtls_ca.CSRValidationError, match="signature"):
        mtls_ca.sign_client_csr(material.ca, tampered_pem)


def test_sign_client_csr_carries_over_requested_san(tmp_path) -> None:
    material = mtls_ca.get_mtls_material(tmp_path / "ca")
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent-with-san")]))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("agent-with-san.internal")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)

    cert_pem = mtls_ca.sign_client_csr(material.ca, csr_pem)
    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "agent-with-san.internal" in san.get_values_for_type(x509.DNSName)


def test_fingerprint_sha256_hex_matches_manual_computation(tmp_path) -> None:
    material = mtls_ca.get_mtls_material(tmp_path / "ca")
    csr_pem = _build_test_csr_pem("fingerprint-test")
    cert_pem = mtls_ca.sign_client_csr(material.ca, csr_pem)
    cert = x509.load_pem_x509_certificate(cert_pem)

    expected = cert.fingerprint(hashes.SHA256()).hex()
    assert mtls_ca.fingerprint_sha256_hex(cert_pem) == expected
    assert len(expected) == 64  # 32 bytes, hex-encoded
