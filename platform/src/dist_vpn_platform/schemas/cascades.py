"""See contracts/rest/openapi.yaml components/schemas/Cascade, CascadeInput, CascadeStatus."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from dist_vpn_platform.models.cascade import CascadeExitLinkStatus, CascadeStatus


class CascadeInput(BaseModel):
    entry_node_id: str
    exit_node_ids: list[str]
    hop_a_presets: list[str] = []


class CascadeExitLinkOut(BaseModel):
    """Per-link view (models/cascade.py CascadeExitLink) - additive alongside the
    older, coarser `CascadeOut.exit_node_ids` (contracts/rest/openapi.yaml's original
    v1 shape, kept as-is rather than broken - see this codebase's own "additive schema
    evolution" convention, e.g. CascadeListResponse.total). exit_node_ids alone cannot
    show per-link status/preset, which matters once a link can be mid-negotiation
    (services/link_negotiation.py, slice 12) while others on the same cascade are UP."""

    model_config = ConfigDict(from_attributes=True)

    remote_exit_candidate_id: str
    remote_node_id: str
    status: CascadeExitLinkStatus
    active_preset: str


class CascadeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entry_node_id: str
    exit_node_ids: list[str]
    exit_links: list[CascadeExitLinkOut] = []
    status: CascadeStatus
    hop_a_presets: list[str]
    # Public REALITY identity for this cascade's Hop A (client-facing) inbound - never
    # the private key (models/cascade.py Cascade.hopa_reality_private_key is never
    # serialized here). Needed by the admin/client to compose a working vless:// URI
    # (see api/routes/users.py's connection endpoint, which composes it server-side).
    hopa_reality_public_key: str | None = None
    hopa_reality_short_id: str | None = None


class CascadeListResponse(BaseModel):
    items: list[CascadeOut]
    # Additive field, not in contracts/rest/openapi.yaml's inline list-response schema
    # for this path (which only declares `items`) - same backward-compatible-extension
    # pattern already used for TokenPair.must_change_password (see
    # platform/README.md "Отклонения от ТЗ"), added for parity with /nodes' list
    # response (which already has `total` in the contract).
    total: int


class CascadeMigrateUsersRequest(BaseModel):
    """See api/routes/cascades.py POST /cascades/{cascade_id}/migrate-users - the
    admin-triggered half of failover (TZ-5 §4.3 "перенаправлять подключения при
    обрыве"): auto-detection (services/health_scheduler.py) marks a cascade DOWN,
    the admin picks a specific healthy replacement and this moves every user over."""

    to_cascade_id: str


class CascadeMigrateUsersResponse(BaseModel):
    cascade_id: str
    to_cascade_id: str
    migrated_user_count: int
