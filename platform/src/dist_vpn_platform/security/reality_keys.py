"""Generates X25519 keypairs and shortIds for Xray-core's REALITY transport
(services/xray_templates.py's Hop A and Hop B inbounds/outbounds).

Previously `services/xray_templates.py` hardcoded `privateKey: ""` / `shortIds: [""]`
with an explicit TODO ("real key material must come from a provisioning step, not yet
wired") - a real REALITY handshake cannot complete with an empty private key, so this
was a hard blocker for any actual client connection, not just a cosmetic gap.

Encoding: Xray-core's REALITY fields are raw 32-byte Curve25519 keys, base64url-encoded
*without* padding - Go's `base64.RawURLEncoding`. Confirmed directly against Xray-core's
source this slice (`main/commands/all/x25519.go`'s `x25519` CLI command): its `-i` flag
is documented as taking `"private key (base64.RawURLEncoding)"`, with a `--std-encoding`
flag offered specifically as the *opt-in* alternative - i.e. RawURLEncoding (no padding)
is the default/expected format REALITY key material is exchanged in, not standard
base64. `cryptography`'s X25519 raw-bytes encoders match this exactly once the trailing
`=` padding that `urlsafe_b64encode` adds is stripped (Python's stdlib has no built-in
"no padding" base64 variant, hence the manual `.rstrip(b"=")` below).
"""
from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_reality_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64), both base64url-nopad-encoded raw
    32-byte X25519 keys, ready to drop into an Xray `realitySettings.privateKey` /
    `realitySettings.publicKey` field."""
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url_nopad(private_raw), _b64url_nopad(public_raw)


def generate_short_id() -> str:
    """8 random bytes as hex (16 chars) - well within Xray's shortIds length limit
    (<=16 bytes / 32 hex chars) and non-empty, unlike the previous `[""]` placeholder."""
    return secrets.token_hex(8)
