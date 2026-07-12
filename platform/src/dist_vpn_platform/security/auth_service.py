"""Login / token-rotation orchestration (TZ-5 §6.3).

Ties security/tokens.py's stateless JWTs to the RefreshToken deny-list table so
refresh tokens can actually be revoked (logout, rotation, theft-detection), not just
left to expire.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dist_vpn_platform.config import settings
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.models.base import as_aware_utc
from dist_vpn_platform.models.refresh_token import RefreshToken
from dist_vpn_platform.security.passwords import hash_password, verify_password
from dist_vpn_platform.security.tokens import TokenError, TokenType, create_access_token, create_refresh_token, decode_token

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised for any auth failure that should surface as HTTP 401 to the caller."""


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    must_change_password: bool


async def issue_token_pair(session: AsyncSession, admin: AdminAccount, *, family_id: str | None = None) -> TokenPair:
    """Issue a fresh access+refresh pair. A fresh `family_id` is started at login; the
    same family_id is threaded through on rotation (see `rotate_refresh_token`)."""
    family_id = family_id or str(uuid.uuid4())

    access_token, _access_jti, access_expires_at = create_access_token(admin.id)
    refresh_token, refresh_jti, refresh_expires_at = create_refresh_token(admin.id, family_id)

    session.add(
        RefreshToken(
            jti=refresh_jti,
            family_id=family_id,
            admin_id=admin.id,
            expires_at=refresh_expires_at,
        )
    )
    await session.flush()

    expires_in = int((access_expires_at - datetime.now(timezone.utc)).total_seconds())
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        must_change_password=admin.must_change_password,
    )


async def rotate_refresh_token(session: AsyncSession, presented_refresh_token: str) -> TokenPair:
    """Validate + rotate a refresh token. Raises AuthError if invalid, expired, already
    revoked, or (theft-detection) already-rotated-and-reused - in the last case the
    entire token family is revoked so a stolen token can't be replayed indefinitely."""
    try:
        decoded = decode_token(presented_refresh_token, expected_type=TokenType.REFRESH)
    except TokenError as exc:
        raise AuthError(str(exc)) from exc

    result = await session.execute(select(RefreshToken).where(RefreshToken.jti == decoded.jti))
    row = result.scalar_one_or_none()
    if row is None:
        raise AuthError("unknown refresh token")

    if row.revoked_at is not None:
        # Reuse of an already-rotated (or already-revoked) token: possible theft.
        # Revoke the whole family so any other outstanding token from this chain dies too.
        logger.warning(
            "refresh_token_reuse_detected",
            extra={"admin_id": row.admin_id, "family_id": row.family_id},
        )
        await _revoke_family(session, row.family_id)
        raise AuthError("refresh token already used - session revoked")

    if as_aware_utc(row.expires_at) < datetime.now(timezone.utc):
        raise AuthError("refresh token expired")

    result = await session.execute(select(AdminAccount).where(AdminAccount.id == row.admin_id))
    admin = result.scalar_one_or_none()
    if admin is None or not admin.is_active:
        raise AuthError("account no longer active")

    new_pair = await issue_token_pair(session, admin, family_id=row.family_id)

    row.revoked_at = datetime.now(timezone.utc)
    # Recover the jti we just embedded in new_pair.refresh_token for bookkeeping.
    new_decoded = decode_token(new_pair.refresh_token, expected_type=TokenType.REFRESH)
    row.replaced_by_jti = new_decoded.jti

    await session.flush()
    return new_pair


async def revoke_refresh_token(session: AsyncSession, presented_refresh_token: str | None) -> None:
    """Used by /auth/logout - revokes just the presented token's row (not idempotent
    failure-free: an already-invalid token is treated as already logged out). A `None`
    token (contract now allows an optional body - see api/routes/auth.py) is a no-op:
    there is no server-side session pointer to fall back to (see platform/README.md
    "Отклонения от ТЗ" #2), so logout without a refresh_token just ends the caller's
    bearer-authenticated request without revoking anything token-specific."""
    if presented_refresh_token is None:
        return
    try:
        decoded = decode_token(presented_refresh_token, expected_type=TokenType.REFRESH)
    except TokenError:
        return
    result = await session.execute(select(RefreshToken).where(RefreshToken.jti == decoded.jti))
    row = result.scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await session.flush()


async def change_password(session: AsyncSession, admin: AdminAccount, *, current_password: str, new_password: str) -> None:
    """POST /auth/change-password: verify `current_password` (Argon2id), set the new
    hash, clear `must_change_password` (TZ-5 §6.3 - "обязательная смена дефолтного
    пароля при первом входе"), and revoke every outstanding refresh-token family for
    this admin so a credential change actually ends any already-issued sessions
    (same rationale as the theft-detection family-revocation in rotate_refresh_token
    above - a password change is exactly the kind of event that should invalidate
    long-lived refresh tokens, not just future logins).

    Raises AuthError (-> 401, per the task's "Return 401 on mismatch") if
    `current_password` doesn't match.
    """
    if not verify_password(current_password, admin.password_hash):
        raise AuthError("current password is incorrect")

    admin.password_hash = hash_password(new_password)
    admin.must_change_password = False

    result = await session.execute(
        select(RefreshToken).where(RefreshToken.admin_id == admin.id, RefreshToken.revoked_at.is_(None))
    )
    now = datetime.now(timezone.utc)
    for row in result.scalars():
        row.revoked_at = now

    await session.flush()


async def _revoke_family(session: AsyncSession, family_id: str) -> None:
    result = await session.execute(select(RefreshToken).where(RefreshToken.family_id == family_id))
    now = datetime.now(timezone.utc)
    for row in result.scalars():
        if row.revoked_at is None:
            row.revoked_at = now
    await session.flush()


def is_account_locked(admin: AdminAccount) -> bool:
    return admin.locked_until is not None and as_aware_utc(admin.locked_until) > datetime.now(timezone.utc)


def register_failed_login(admin: AdminAccount) -> None:
    """Per-account lockout (TZ-5 §6.3), independent of the per-IP slowapi limit on the
    route itself."""
    admin.failed_login_attempts += 1
    if admin.failed_login_attempts >= settings.login_max_failed_attempts:
        from datetime import timedelta

        admin.locked_until = datetime.now(timezone.utc) + timedelta(minutes=settings.login_lockout_minutes)
        logger.warning("admin_account_locked", extra={"admin_id": admin.id})


def register_successful_login(admin: AdminAccount) -> None:
    admin.failed_login_attempts = 0
    admin.locked_until = None
    admin.last_login_at = datetime.now(timezone.utc)
