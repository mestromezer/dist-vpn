"""security/reality_keys.py - REALITY X25519 keypair/shortId generation."""
from __future__ import annotations

import base64

from dist_vpn_platform.security import reality_keys


def _b64url_nopad_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


def test_generate_reality_keypair_produces_32_byte_raw_x25519_keys() -> None:
    private_key_b64, public_key_b64 = reality_keys.generate_reality_keypair()

    assert "=" not in private_key_b64  # RawURLEncoding, no padding (Xray's x25519 default)
    assert "=" not in public_key_b64
    assert _b64url_nopad_decode(private_key_b64).__len__() == 32
    assert _b64url_nopad_decode(public_key_b64).__len__() == 32
    assert private_key_b64 != public_key_b64


def test_generate_reality_keypair_is_not_reused_across_calls() -> None:
    first_private, first_public = reality_keys.generate_reality_keypair()
    second_private, second_public = reality_keys.generate_reality_keypair()

    assert first_private != second_private
    assert first_public != second_public


def test_public_key_actually_derives_from_the_returned_private_key() -> None:
    """Round-trips the private key back through cryptography's own X25519 loader and
    confirms it derives the same public key we returned - catches a mismatched-encoding
    bug (e.g. accidentally returning two independently generated keys) that a bare
    length/format check above would miss."""
    from cryptography.hazmat.primitives.asymmetric import x25519

    private_key_b64, public_key_b64 = reality_keys.generate_reality_keypair()
    private_raw = _b64url_nopad_decode(private_key_b64)
    public_raw = _b64url_nopad_decode(public_key_b64)

    loaded_private = x25519.X25519PrivateKey.from_private_bytes(private_raw)
    derived_public_raw = loaded_private.public_key().public_bytes_raw()

    assert derived_public_raw == public_raw


def test_generate_short_id_is_valid_hex_within_xray_length_limit() -> None:
    short_id = reality_keys.generate_short_id()

    assert len(short_id) == 16  # 8 bytes as hex
    int(short_id, 16)  # raises ValueError if not valid hex
    assert short_id != reality_keys.generate_short_id()
