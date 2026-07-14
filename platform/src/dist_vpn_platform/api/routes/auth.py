"""POST /auth/login, /auth/refresh, /auth/logout - contracts/rest/openapi.yaml.

Security baseline implemented per TZ-5 §6.3 (not deferred): Argon2id verification,
per-account lockout + per-IP slowapi rate limiting on /auth/login, JWT access+refresh
with rotation and a DB-backed deny-list, audit logging of login attempts.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.config import settings
from dist_vpn_platform.db import get_db
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenPairResponse,
)
from dist_vpn_platform.security.auth_service import (
    AuthError,
    change_password,
    is_account_locked,
    issue_token_pair,
    register_failed_login,
    register_successful_login,
    revoke_refresh_token,
    rotate_refresh_token,
)
from dist_vpn_platform.security.passwords import verify_password
from dist_vpn_platform.security.rate_limit import limiter
from dist_vpn_platform.security.totp import verify_code

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("dist_vpn_platform.audit")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPairResponse)
@limiter.limit(settings.login_rate_limit_per_ip)
async def login(
    request: Request,  # required by slowapi to key on client IP
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenPairResponse:
    result = await db.execute(select(AdminAccount).where(AdminAccount.username == body.username))
    admin = result.scalar_one_or_none()

    # Constant-shape failure path: don't reveal whether the username exists.
    if admin is None:
        audit_logger.warning("login_failed", extra={"username": body.username, "reason": "unknown_user"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    if is_account_locked(admin):
        audit_logger.warning("login_failed", extra={"username": body.username, "reason": "account_locked"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account temporarily locked - too many failed attempts")

    if not admin.is_active or not verify_password(body.password, admin.password_hash):
        register_failed_login(admin)
        await db.commit()
        audit_logger.warning("login_failed", extra={"username": body.username, "reason": "bad_credentials"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    if admin.totp_enabled and not verify_code(admin.totp_secret or "", body.totp_code or ""):
        register_failed_login(admin)
        await db.commit()
        audit_logger.warning("login_failed", extra={"username": body.username, "reason": "bad_totp"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing TOTP code")

    register_successful_login(admin)
    pair = await issue_token_pair(db, admin)
    await db.commit()

    audit_logger.info("login_succeeded", extra={"username": body.username, "admin_id": admin.id})
    return TokenPairResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        must_change_password=pair.must_change_password,
    )


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenPairResponse:
    try:
        pair = await rotate_refresh_token(db, body.refresh_token)
    except AuthError as exc:
        await db.commit()  # persist any family-revocation performed on the failure path
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    await db.commit()
    return TokenPairResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        must_change_password=pair.must_change_password,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest | None = None,
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> Response:
    # NOTE: contracts/rest/openapi.yaml declares no requestBody for this path (v0.1
    # draft gap - flagged, not silently changed - see platform/README.md). We require
    # the caller to still be bearer-authenticated (global `security: bearerAuth`
    # applies since this path doesn't override it); the refresh_token body is now
    # optional (a bare `POST /auth/logout` with no body is accepted) but only an
    # actually-supplied refresh_token gets revoked - see revoke_refresh_token's
    # docstring for why there's no session-independent way to do more than that.
    await revoke_refresh_token(db, body.refresh_token if body is not None else None)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password_route(
    body: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> Response:
    # Not in contracts/rest/openapi.yaml v0.1 (checked - no /auth/change-password path
    # exists there as of this slice) - an additive new endpoint, same "flag it, don't
    # silently break the contract" posture as the other deviations in this file/README,
    # wiring up the must_change_password field that was otherwise an orphaned addition
    # from the previous slice (see schemas/auth.py TokenPairResponse).
    admin_id = admin.id  # captured before any rollback expires the ORM object below
    try:
        await change_password(db, admin, current_password=body.current_password, new_password=body.new_password)
    except AuthError as exc:
        await db.rollback()
        audit_logger.warning("change_password_failed", extra={"admin_id": admin_id})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    await db.commit()
    audit_logger.info("change_password_succeeded", extra={"admin_id": admin_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
