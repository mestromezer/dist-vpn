"""Platform<->Platform peering identity - mutual CERT PINNING, deliberately not a CA
hierarchy like security/mtls_ca.py (see contracts/proto/platform_peering.proto's
header comment for the full "why not reuse the Agent-facing CA" reasoning: a static,
operator-established, exactly-two-parties relationship is the wrong shape for that
CA's 90-day auto-issued-off-a-one-time-token design, and reusing one CA for both would
make a compromised Agent enrollment flow equal-trust to "the other Platform").

Each Platform instance generates ONE self-signed identity for itself (no separate
CA/leaf split needed - there is no third party ever expected to trust this
certificate, only the one specific peer it gets pinned into). The operator copies the
resulting certificate PEM out-of-band (same "print once, plainly, outside the JSON
logger" pattern as security/mtls_ca.py's one-time CA message) into the peer's
`PeerPlatform.peer_cert_pem` column (models/peer_platform.py) at provisioning time -
there is no issuance flow and nothing to renew on an expiry schedule the way
mtls_ca.py's short-lived Agent client certs are, so this identity is long-lived by
design (matching the CA root's own validity period below, not the short-lived leaf).

Same EC P-256/ECDSA-SHA256 choice as security/mtls_ca.py, for the same reasons
(fast keygen/signing, universal support in grpc's BoringSSL-based TLS stack).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PEERING_IDENTITY_VALIDITY_DAYS = 3650  # long-lived, no renewal flow - see module docstring
_NOT_BEFORE_SKEW = timedelta(minutes=5)  # tolerate modest clock drift, same as mtls_ca.py


@dataclasses.dataclass(frozen=True)
class PeeringIdentity:
    key_pem: str
    cert_pem: str


def generate_peering_identity(*, common_name: str) -> PeeringIdentity:
    """Generates a new, self-signed peering identity. Callers are responsible for
    persisting the result (PeerPlatform.local_peering_key_pem/local_peering_cert_pem)
    - this function has no side effects and does not cache, unlike mtls_ca.py's
    get_mtls_material() (there is exactly one identity per Platform instance, created
    once at provisioning time via a CLI/admin action, not re-derived on every gRPC
    connection - no hot-path reason to cache)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _NOT_BEFORE_SKEW)
        .not_valid_after(now + timedelta(days=PEERING_IDENTITY_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return PeeringIdentity(key_pem=key_pem, cert_pem=cert_pem)
