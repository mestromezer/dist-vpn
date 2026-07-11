"""JWT encode/decode helpers (TZ-5 §6.3).

Pure functions only - no DB access here. The higher-level issue/rotate/revoke flow
that ties these claims to the `RefreshToken` deny-list table lives in
`security/auth_service.py`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import jwt

from dist_vpn_platform.config import settings


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    """Raised for any invalid/expired/malformed token - callers should treat this as 401."""


@dataclass(frozen=True)
class DecodedToken:
    sub: str  # AdminAccount.id
    jti: str
    token_type: TokenType
    family_id: str | None = None


def _encode(*, sub: str, jti: str, token_type: TokenType, expires_at: datetime, family_id: str | None) -> str:
    payload: dict = {
        "sub": sub,
        "jti": jti,
        "type": token_type.value,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if family_id is not None:
        payload["fam"] = family_id
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(admin_id: str) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at)."""
    jti = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_ttl_minutes)
    token = _encode(sub=admin_id, jti=jti, token_type=TokenType.ACCESS, expires_at=expires_at, family_id=None)
    return token, jti, expires_at


def create_refresh_token(admin_id: str, family_id: str) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at)."""
    jti = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_ttl_days)
    token = _encode(
        sub=admin_id, jti=jti, token_type=TokenType.REFRESH, expires_at=expires_at, family_id=family_id
    )
    return token, jti, expires_at


def decode_token(token: str, *, expected_type: TokenType) -> DecodedToken:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc

    if payload.get("type") != expected_type.value:
        raise TokenError(f"expected token type {expected_type.value!r}")

    sub = payload.get("sub")
    jti = payload.get("jti")
    if not sub or not jti:
        raise TokenError("token missing required claims")

    return DecodedToken(
        sub=sub, jti=jti, token_type=expected_type, family_id=payload.get("fam")
    )
