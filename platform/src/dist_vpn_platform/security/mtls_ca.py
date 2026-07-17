"""Platform's private mTLS Certificate Authority (TZ-5 §6.2).

This slice implements the CA in-process with the `cryptography` library rather than
delegating to step-ca (an external service, see docs/TZ-5-platform.md §2's table entry
recommending it) - a documented, valid future upgrade, out of scope here. Everything
outside this module (grpc_server/server.py, grpc_server/servicer.py) only sees
`CAMaterial` / `ServerIdentity` / `sign_client_csr()`, not how they are produced, so
swapping in a real step-ca client later does not require touching call sites.

Key algorithm and validity periods - documented here because the independently-written
Agent track does not see this file and needs these facts to interoperate:

  - CA and every certificate it issues (server leaf + Agent client certs) use EC
    (SECP256R1 / P-256) keys and ECDSA-SHA256 signatures. Chosen over RSA-4096 for
    faster keygen/signing (CSR signing runs inline inside the gRPC Enrollment handler,
    on the request path) and because P-256 is universally supported by grpc's
    BoringSSL-based TLS stack and by OpenSSL. The Agent's own CSR key algorithm is the
    Agent's own choice (grpc/cryptography.x509 do not require the CSR key algorithm to
    match the signing CA's) - this module signs whatever public key is in a
    successfully-validated CSR, it does not require EC specifically from the Agent.
  - CA root certificate: 3650 days (10 years) - long-lived by design; this is the
    trust anchor operators distribute out-of-band (see `_load_or_create_ca`'s one-time
    console message), so rotating it means re-distributing to every Agent - deliberately
    not short-lived like the leaf certs it signs.
  - Server leaf certificate (Platform's own gRPC server identity): 825 days.
  - Agent client certificate (issued per Enrollment from a submitted CSR): 90 days,
    per TZ-5 §6.2 ("short-lived with auto-renewal"). Automated renewal itself
    (re-enrollment or a dedicated renew RPC before expiry) is a follow-up-slice TODO -
    see platform/README.md.

Persistence: both the CA keypair/cert and the server leaf keypair/cert are written to
`settings.mtls_ca_dir` (default `var/ca` under the working directory for local/non-Docker
dev; a Docker named volume mounted at `/var/lib/dist-vpn-platform/ca` in
docker-compose.yml for the containerized stack) so they survive process restarts -
generation only happens once, the first time this module finds nothing on disk.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from dist_vpn_platform.config import settings

logger = logging.getLogger(__name__)

CA_KEY_FILENAME = "ca-key.pem"
CA_CERT_FILENAME = "ca-cert.pem"
SERVER_KEY_FILENAME = "server-key.pem"
SERVER_CERT_FILENAME = "server-cert.pem"

CA_SUBJECT_CN = "dist-vpn-platform Root CA"
CA_CERT_VALIDITY_DAYS = 3650
SERVER_CERT_VALIDITY_DAYS = 825
CLIENT_CERT_VALIDITY_DAYS = 90
# Tolerate modest clock drift between Platform and Agent hosts (not-yet-valid errors
# on a freshly-issued cert are a worse failure mode than a few minutes of overlap).
_NOT_BEFORE_SKEW = timedelta(minutes=5)


class CSRValidationError(ValueError):
    """Raised by sign_client_csr() when Enrollment.csr_pem is missing, unparsable, or
    fails its own self-signature check - the caller (grpc_server/servicer.py
    _handle_enrollment) turns this into an EnrollmentAck(accepted=False, ...) rather
    than letting it propagate as a 5xx/crash."""


@dataclasses.dataclass(frozen=True)
class CAMaterial:
    private_key: ec.EllipticCurvePrivateKey
    certificate: x509.Certificate

    @property
    def cert_pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)


@dataclasses.dataclass(frozen=True)
class ServerIdentity:
    key_pem: bytes
    cert_pem: bytes


@dataclasses.dataclass(frozen=True)
class MTLSMaterial:
    ca: CAMaterial
    server: ServerIdentity


# In-process cache keyed by resolved ca_dir path, so repeated calls (e.g. one per
# incoming gRPC connection) don't re-read the filesystem every time. Generation itself
# is disk-persisted (see module docstring), so this cache is purely a hot-path
# optimization, not the source of truth.
_cache: dict[str, MTLSMaterial] = {}
_cache_lock = threading.Lock()


def _write_private_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    try:
        # 0600 - best-effort. Windows dev boxes use ACLs instead of POSIX mode bits;
        # os.chmod there does not raise for this, it just doesn't restrict access the
        # same way - acceptable for local dev, real deployments run this in the Linux
        # container image (see platform/Dockerfile).
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        logger.warning("mtls_key_chmod_failed", extra={"path": str(path)})


def _load_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise RuntimeError(f"{path}: expected an EC private key, found {type(key).__name__}")
    return key


def _build_ca_certificate(key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, CA_SUBJECT_CN),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "dist-vpn"),
        ]
    )
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _NOT_BEFORE_SKEW)
        .not_valid_after(now + timedelta(days=CA_CERT_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    return builder.sign(key, hashes.SHA256())


def _load_or_create_ca(ca_dir: Path) -> CAMaterial:
    key_path = ca_dir / CA_KEY_FILENAME
    cert_path = ca_dir / CA_CERT_FILENAME

    if key_path.exists() and cert_path.exists():
        key = _load_private_key(key_path)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        logger.info("mtls_ca_loaded", extra={"ca_dir": str(ca_dir)})
        return CAMaterial(private_key=key, certificate=cert)

    ca_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _build_ca_certificate(key)
    _write_private_key(key_path, key)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path.write_bytes(cert_pem)

    # Same "print once, plainly, outside the JSON logger" pattern as seed.py's
    # default-admin-password-on-first-boot flow (TZ-5 §6.3) - an operator must
    # actively notice and act on this, not grep it out of shipped JSON logs later.
    print("=" * 72)
    print("Generated a new mTLS root CA for this Platform instance.")
    print(f"CA certificate (public, safe to distribute): {cert_path}")
    print(
        "Copy this file out and distribute it to Agents via the provisioning flow's "
        "existing 'agent_mtls_ca_cert_src' vault variable (see provisioning/README.md, "
        "TZ-7) so Agents can verify the Platform's gRPC server and build trust for "
        "their own enrollment. The CA PRIVATE KEY never leaves this volume - do not "
        "copy ca-key.pem anywhere."
    )
    print("This message is only printed once, at first generation.")
    print("=" * 72)
    logger.info("mtls_ca_generated", extra={"ca_dir": str(ca_dir), "cert_path": str(cert_path)})

    return CAMaterial(private_key=key, certificate=cert)


def _san_from_hostnames(hostnames: list[str]) -> x509.SubjectAlternativeName:
    names: list[x509.GeneralName] = []
    for host in hostnames:
        host = host.strip()
        if not host:
            continue
        try:
            names.append(x509.IPAddress(ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return x509.SubjectAlternativeName(names)


def _build_server_certificate(
    ca: CAMaterial, key: ec.EllipticCurvePrivateKey, hostnames: list[str]
) -> x509.Certificate:
    common_name = hostnames[0] if hostnames else "localhost"
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca.certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _NOT_BEFORE_SKEW)
        .not_valid_after(now + timedelta(days=SERVER_CERT_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(_san_from_hostnames(hostnames), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.private_key.public_key()), critical=False
        )
    )
    return builder.sign(ca.private_key, hashes.SHA256())


def _load_or_create_server_identity(ca_dir: Path, ca: CAMaterial, hostnames: list[str]) -> ServerIdentity:
    key_path = ca_dir / SERVER_KEY_FILENAME
    cert_path = ca_dir / SERVER_CERT_FILENAME

    if key_path.exists() and cert_path.exists():
        logger.info("mtls_server_identity_loaded", extra={"ca_dir": str(ca_dir)})
        return ServerIdentity(key_pem=key_path.read_bytes(), cert_pem=cert_path.read_bytes())

    key = ec.generate_private_key(ec.SECP256R1())
    cert = _build_server_certificate(ca, key, hostnames)
    _write_private_key(key_path, key)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path.write_bytes(cert_pem)
    logger.info(
        "mtls_server_identity_generated",
        extra={"ca_dir": str(ca_dir), "sans": hostnames, "cert_path": str(cert_path)},
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return ServerIdentity(key_pem=key_pem, cert_pem=cert_pem)


def _default_server_sans() -> list[str]:
    return [h.strip() for h in settings.mtls_server_sans.split(",") if h.strip()]


def get_mtls_material(ca_dir: str | Path | None = None, *, server_sans: list[str] | None = None) -> MTLSMaterial:
    """Idempotent entrypoint used by both grpc_server/server.py (to build
    ssl_server_credentials) and grpc_server/servicer.py (to sign Enrollment CSRs).

    On the first call for a given `ca_dir`, generates + persists the CA and server
    leaf cert if nothing is on disk yet (see module docstring for validity periods);
    every subsequent call for the same `ca_dir` - including across process restarts,
    since persistence is disk-based, not just the in-process cache - loads the
    existing material instead of regenerating it.
    """
    resolved_dir = Path(ca_dir if ca_dir is not None else settings.mtls_ca_dir).resolve()
    cache_key = str(resolved_dir)

    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        ca = _load_or_create_ca(resolved_dir)
        hostnames = server_sans if server_sans is not None else _default_server_sans()
        server = _load_or_create_server_identity(resolved_dir, ca, hostnames)
        material = MTLSMaterial(ca=ca, server=server)
        _cache[cache_key] = material
        return material


def reset_cache_for_tests() -> None:
    """Test-only escape hatch (see tests/conftest.py's per-test `mtls_ca_dir`
    fixture, same isolation pattern as db_engine's per-test sqlite file) - clears the
    in-process cache so each test starts from a clean slate for its own tmp ca_dir.
    Never touches disk."""
    with _cache_lock:
        _cache.clear()


def sign_client_csr(ca: CAMaterial, csr_pem: bytes, *, validity_days: int = CLIENT_CERT_VALIDITY_DAYS) -> bytes:
    """Validate and sign a PKCS#10 CSR (contracts/proto/agent_control.proto
    Enrollment.csr_pem) into a short-lived Agent mTLS client certificate.

    Raises CSRValidationError (never a bare exception) if csr_pem is empty, cannot be
    parsed as a PEM-encoded CSR, or its embedded self-signature does not verify - the
    caller (grpc_server/servicer.py _handle_enrollment) is expected to catch this and
    turn it into EnrollmentAck(accepted=False, reason=...) without burning the
    enrollment token or creating a Node row.
    """
    if not csr_pem:
        raise CSRValidationError("csr_pem is required")

    try:
        csr = x509.load_pem_x509_csr(csr_pem)
    except ValueError as exc:
        raise CSRValidationError(f"malformed CSR: {exc}") from exc

    try:
        signature_ok = csr.is_signature_valid
    except Exception as exc:  # noqa: BLE001 - unsupported key/algorithm, corrupt ASN.1, etc.
        raise CSRValidationError(f"could not verify CSR signature: {exc}") from exc
    if not signature_ok:
        raise CSRValidationError("CSR self-signature does not verify")

    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca.certificate.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _NOT_BEFORE_SKEW)
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.private_key.public_key()), critical=False
        )
    )

    try:
        san_ext = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        pass
    else:
        # Carried over as-is if the Agent's CSR requested one - not required for
        # client-auth verification (grpc's require_client_auth only checks the chain
        # of trust, not a hostname match, for client certs), but harmless to preserve
        # for anything downstream that inspects the issued cert.
        builder = builder.add_extension(san_ext.value, critical=False)

    certificate = builder.sign(ca.private_key, hashes.SHA256())
    return certificate.public_bytes(serialization.Encoding.PEM)


def fingerprint_sha256_hex(cert_pem: bytes) -> str:
    """SHA-256 fingerprint (hex) of an issued certificate - stored on
    Node.mtls_cert_fingerprint (models/node.py, column already existed as a documented
    TODO placeholder before this slice)."""
    certificate = x509.load_pem_x509_certificate(cert_pem)
    return certificate.fingerprint(hashes.SHA256()).hex()


def verify_client_cert_pem(ca: CAMaterial, cert_pem: bytes) -> bool:
    """True only if `cert_pem` is a certificate this CA itself issued (signature
    chains directly to `ca.certificate`) and is currently within its validity period.

    Needed because grpc_server/server.py binds with `require_client_auth=False` (see
    its module docstring) - found, while wiring that up, that grpc's C-core does not
    verify an *offered* client cert's chain of trust at all in that mode (that's
    `GRPC_SSL_REQUEST_CLIENT_CERTIFICATE_BUT_DONT_VERIFY`, not "verify if offered" -
    `require_client_auth=True` is the only mode that gets chain verification from
    grpc/BoringSSL itself). `grpc_server/servicer.py::Session` calls this explicitly on
    whatever PEM cert `context.auth_context()` reports for the peer, rather than
    trusting `peer_identities()` alone, which only reflects "a cert was offered", not
    "and it was ours"."""
    try:
        certificate = x509.load_pem_x509_certificate(cert_pem)
        certificate.verify_directly_issued_by(ca.certificate)
    except (ValueError, TypeError, InvalidSignature):
        return False
    now = datetime.now(timezone.utc)
    return certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc
