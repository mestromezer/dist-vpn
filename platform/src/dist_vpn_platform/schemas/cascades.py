"""See contracts/rest/openapi.yaml components/schemas/Cascade, CascadeInput, CascadeStatus."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from dist_vpn_platform.models.cascade import CascadeStatus


class CascadeInput(BaseModel):
    entry_node_id: str
    exit_node_ids: list[str]
    hop_a_presets: list[str] = []


class CascadeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entry_node_id: str
    exit_node_ids: list[str]
    status: CascadeStatus
    hop_a_presets: list[str]


class CascadeListResponse(BaseModel):
    items: list[CascadeOut]
    # Additive field, not in contracts/rest/openapi.yaml's inline list-response schema
    # for this path (which only declares `items`) - same backward-compatible-extension
    # pattern already used for TokenPair.must_change_password (see
    # platform/README.md "Отклонения от ТЗ"), added for parity with /nodes' list
    # response (which already has `total` in the contract).
    total: int
