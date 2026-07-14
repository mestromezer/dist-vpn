"""Request/response models for /auth/* - see contracts/rest/openapi.yaml paths
/auth/login, /auth/refresh, /auth/logout and components/schemas/TokenPair."""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str | None = Field(default=None, description="TOTP code, if MFA is enabled (TZ-5 §6.3)")


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    # Optional per this slice's reading of the contract gap (see platform/README.md
    # "Отклонения от ТЗ" #2) - if omitted, we still revoke on a best-effort basis by
    # doing nothing token-specific (there is no session-id to fall back to without a
    # server-side session pointer, which this design deliberately doesn't have - see
    # the same README note). Passing it is what actually revokes something.
    refresh_token: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    # Additive field, not in the openapi.yaml v0.1 TokenPair schema (which has no
    # `additionalProperties: false`, so this is a backward-compatible extension) -
    # see platform/README.md "Deviations" for why this exists ahead of a dedicated
    # change-password endpoint.
    must_change_password: bool = False
