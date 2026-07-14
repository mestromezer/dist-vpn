"""See contracts/rest/openapi.yaml components/schemas/User and paths /users, /users/{user_id}."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserCreateRequest(BaseModel):
    name: str
    cascade_ids: list[str]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    uuid: str
    cascade_ids: list[str]
    created_at: datetime
    revoked_at: datetime | None = None


class UserListResponse(BaseModel):
    items: list[UserOut]
    # Additive field - see the identical note on CascadeListResponse.total
    # (schemas/cascades.py) for why this isn't literally in openapi.yaml yet.
    total: int


class UserConnectionOut(BaseModel):
    """See api/routes/users.py GET /users/{user_id}/connection/{cascade_id} - not in
    contracts/rest/openapi.yaml (additive, same posture as /auth/change-password, see
    platform/README.md "Отклонения от ТЗ")."""

    vless_uri: str
