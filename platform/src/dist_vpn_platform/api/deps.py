"""Shared FastAPI dependencies: DB session + authenticated-admin extraction.

TZ-5 §6.3 - Broken Access Control (OWASP A01): every protected route depends
explicitly on `require_admin` (or a future `require_role(...)`) rather than relying
on the Admin Client hiding UI affordances.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dist_vpn_platform.db import get_db
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.security.tokens import TokenError, TokenType, decode_token

# auto_error=False so we can return a uniform 401 body instead of FastAPI's default.
_bearer_scheme = HTTPBearer(auto_error=False)


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> AdminAccount:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    try:
        decoded = decode_token(credentials.credentials, expected_type=TokenType.ACCESS)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired access token") from exc

    result = await db.execute(select(AdminAccount).where(AdminAccount.id == decoded.sub))
    admin = result.scalar_one_or_none()
    if admin is None or not admin.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found or disabled")

    return admin
