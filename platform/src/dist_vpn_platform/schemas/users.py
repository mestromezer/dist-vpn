"""See contracts/rest/openapi.yaml components/schemas/User and paths /users, /users/{user_id}."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from dist_vpn_platform.models.user import DEFAULT_MAX_DEVICES


class UserCreateRequest(BaseModel):
    name: str
    cascade_ids: list[str]
    # None (the default if omitted) means DEFAULT_MAX_DEVICES, NOT unlimited - an
    # admin who actually wants unlimited must explicitly pass max_devices: null in
    # the body after creation via PATCH (see UserPatchRequest below), or extend this
    # endpoint later if "unlimited at creation time" turns out to be a real need this
    # slice didn't anticipate.
    max_devices: int | None = DEFAULT_MAX_DEVICES


class UserPatchRequest(BaseModel):
    """Not in contracts/rest/openapi.yaml - PATCH /users/{user_id} didn't exist at
    all before this slice (only GET/POST/DELETE), same additive-and-documented
    posture as PATCH /nodes/{id}'s public_address field and other deviations, see
    platform/README.md "Отклонения от ТЗ". max_devices: null explicitly means
    unlimited (distinct from omitting the field, which leaves it unchanged)."""

    max_devices: int | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    uuid: str
    cascade_ids: list[str]
    max_devices: int | None
    last_online_device_count: int
    last_online_at: datetime | None
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
    # Second URI for the optional P4 preset (VLESS+TLS/WebSocket) - present only when
    # the cascade actually requested "p4" (Cascade.hop_a_presets) AND the entry node
    # has Node.p4_domain set; None otherwise, not an empty string, so a client can
    # distinguish "P4 wasn't requested/configured" from "P4 URI happens to be empty".
    vless_uri_p4: str | None = None
    # Xray-compatible routing rule objects (direct-route local resources, block known
    # IP-checking services) a client can drop into its own config - see
    # services/xray_templates.py build_client_routing_rules().
    routing_rules: list[dict] = []


class UserDeviceKickEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    node_id: str
    online_ip_count: int
    max_devices: int
    kicked_at: datetime


class UserDeviceKickEventListResponse(BaseModel):
    items: list[UserDeviceKickEventOut]
    total: int
