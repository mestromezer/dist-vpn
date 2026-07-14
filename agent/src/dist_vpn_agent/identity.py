"""Local node mTLS identity: EC (P-256) keypair + CSR generation, and
persistence of the certificate Platform issues in response, to
``AgentSettings.agent_state_dir``.

Closes the TODO flagged at the end of the previous sub-slice (see
agent/README.md's former "Scope of this slice" / "Замеченные вопросы к
контракту" sections, now moved to "done"): ``Enrollment.csr_pem`` existed in
the contract but nothing generated a keypair or filled it in, and
``EnrollmentAck.mtls_client_cert_pem`` was only logged, never persisted.

--- Design (read this if you are the sibling session building Platform's CA
    signing path against the same contract and can't see this file) ---

1. Bootstrap (no persisted identity yet): ``control_client.py`` calls
   :func:`generate_csr` with ``common_name=<agent hostname>`` right before
   building the ``Enrollment`` message. This generates a fresh EC/P-256
   keypair *in memory only* and a PKCS#10 CSR signed by it. The private key
   is kept on the ``ControlClient`` instance (never written to disk, never
   put on the wire) until a matching certificate comes back.
2. On ``EnrollmentAck`` with a non-empty ``mtls_client_cert_pem``,
   ``control_client.py`` calls :func:`persist_identity` with that exact
   in-memory private key (PKCS8 PEM, unencrypted) + the returned cert bytes
   + the assigned ``node_id``, writing three files under
   ``agent_state_dir`` (see the module-level filename constants):
   the private key at ``0600``, the certificate at ``0644`` (it's not a
   secret), and the assigned node_id as plain text at ``0644`` (needed so a
   later reconnect can send a ``Heartbeat.node_id`` without having to ask
   Platform who it is again).
3. On every subsequent ``run_session()`` call (a fresh process start *or* an
   in-process reconnect after a dropped stream -- see ``run_forever()``),
   :func:`load_persisted_identity` is checked first. If all three files are
   present, ``control_client.py`` uses the persisted cert/key as the mTLS
   client identity for the channel (instead of
   ``AgentSettings.mtls_client_cert_path``/``mtls_client_key_path``, which
   remain the operator-supplied override for cases outside this flow, e.g.
   a cert provisioned entirely out-of-band) *and* skips sending a fresh
   ``Enrollment`` -- see control_client.py's "Heartbeat-first reconnect"
   design note for why and how that's signaled to Platform.

Chmod note: file permissions (0600/0644) are only meaningfully enforced on
POSIX (this Agent's actual deployment target is a Linux container, TZ-4
SS5.3). ``Path.chmod`` on Windows does not implement Unix permission bits;
:func:`_write_with_mode` still calls it there (harmless no-op-ish behavior)
so the exact same code path runs in dev/test and production rather than
branching on platform, but don't read anything into chmod's return value on
Windows.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

# Filenames under agent_state_dir. Deliberately plain, separate files (not one
# bundled JSON blob) so the private key and certificate keep independently
# meaningful Unix permissions and can be inspected/copied with standard PEM
# tooling if an operator ever needs to.
_PRIVATE_KEY_FILENAME = "client.key.pem"
_CERT_FILENAME = "client.cert.pem"
_NODE_ID_FILENAME = "node_id"

_PRIVATE_KEY_MODE = 0o600  # secret -- never transmitted, see module docstring
_CERT_MODE = 0o644  # not a secret, it's a certificate
_NODE_ID_MODE = 0o644  # not a secret, just an assigned identifier


@dataclasses.dataclass(frozen=True)
class GeneratedCsr:
    """A freshly generated, not-yet-persisted local identity: an in-memory
    EC/P-256 private key plus the CSR built from it. ``private_key_pem`` is
    kept only in memory by the caller (``ControlClient``) until a matching
    ``EnrollmentAck.mtls_client_cert_pem`` arrives -- see
    :func:`persist_identity`. Never logged, never written to disk on its
    own.
    """

    private_key_pem: bytes  # PKCS8, unencrypted PEM
    csr_pem: bytes  # PKCS#10 CSR, PEM


def generate_csr(*, common_name: str) -> GeneratedCsr:
    """Generate a fresh EC (P-256 / SECP256R1) keypair and a PKCS#10 CSR for
    it, Subject CN = ``common_name`` (the agent's hostname is fine --
    Platform assigns the real ``node_id`` separately via ``EnrollmentAck``;
    the CSR's CN does not need to match it, see agent_control.proto's
    comment on ``Enrollment.csr_pem``).
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(private_key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)

    logger.info("generated local EC/P-256 keypair + CSR (CN=%s)", common_name)
    return GeneratedCsr(private_key_pem=private_key_pem, csr_pem=csr_pem)


@dataclasses.dataclass(frozen=True)
class PersistedIdentity:
    """A previously persisted node identity, read back from
    ``agent_state_dir``. ``cert_path``/``key_path`` are handed directly to
    ``grpc.ssl_channel_credentials`` (via reading the bytes) as the mTLS
    client certificate chain/private key.
    """

    node_id: str
    cert_path: Path
    key_path: Path


def load_persisted_identity(state_dir: Path) -> PersistedIdentity | None:
    """Return the persisted identity in ``state_dir``, or ``None`` if it's
    not (fully) there yet -- e.g. first-ever boot, or a state directory that
    was wiped. Deliberately all-or-nothing: a directory with only a cert but
    no key (or vice versa) is not usable as an mTLS identity, so it's
    treated the same as "nothing persisted" rather than raising -- the
    caller's fallback is always "enroll with the token", which is the safe
    thing to do here regardless of *why* the identity is incomplete.
    """
    cert_path = state_dir / _CERT_FILENAME
    key_path = state_dir / _PRIVATE_KEY_FILENAME
    node_id_path = state_dir / _NODE_ID_FILENAME

    if not (cert_path.is_file() and key_path.is_file() and node_id_path.is_file()):
        return None

    node_id = node_id_path.read_text(encoding="utf-8").strip()
    if not node_id:
        logger.warning(
            "state dir %s has a persisted cert+key but an empty node_id file -- "
            "treating as no persisted identity",
            state_dir,
        )
        return None

    return PersistedIdentity(node_id=node_id, cert_path=cert_path, key_path=key_path)


def persist_identity(state_dir: Path, *, node_id: str, private_key_pem: bytes, cert_pem: bytes) -> PersistedIdentity:
    """Write the private key (0600), certificate (0644), and node_id (0644)
    to ``state_dir``, creating it if needed. Overwrites any previous
    identity in that directory outright -- there is exactly one identity per
    Agent process/state dir, and a fresh ``EnrollmentAck`` always supersedes
    whatever was there before (e.g. after an operator wipes and re-enrolls a
    node).
    """
    state_dir.mkdir(parents=True, exist_ok=True)

    key_path = state_dir / _PRIVATE_KEY_FILENAME
    cert_path = state_dir / _CERT_FILENAME
    node_id_path = state_dir / _NODE_ID_FILENAME

    _write_with_mode(key_path, private_key_pem, mode=_PRIVATE_KEY_MODE)
    _write_with_mode(cert_path, cert_pem, mode=_CERT_MODE)
    _write_with_mode(node_id_path, node_id.encode("utf-8"), mode=_NODE_ID_MODE)

    logger.info(
        "persisted mTLS client identity to %s (node_id=%s, cert=%d bytes)",
        state_dir,
        node_id,
        len(cert_pem),
    )
    return PersistedIdentity(node_id=node_id, cert_path=cert_path, key_path=key_path)


def _write_with_mode(path: Path, data: bytes, *, mode: int) -> None:
    path.write_bytes(data)
    with contextlib_suppress_os_error():
        path.chmod(mode)


def contextlib_suppress_os_error():
    import contextlib

    return contextlib.suppress(OSError)
