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


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    # Additive field, not in the openapi.yaml v0.1 TokenPair schema (which has no
    # `additionalProperties: false`, so this is a backward-compatible extension) -
    # see platform/README.md "Deviations" for why this exists ahead of a dedicated
    # change-password endpoint.
    must_change_password: bool = False
