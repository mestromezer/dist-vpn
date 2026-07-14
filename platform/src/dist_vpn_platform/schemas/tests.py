"""See contracts/rest/openapi.yaml components/schemas/LinkTest, TestOutcome and
paths /cascades/{id}/test, /tests/{test_id}."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from dist_vpn_platform.models.link_test import TestOutcome


class LinkTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cascade_id: str
    outcome: TestOutcome | None = None
    latency_ms: int | None = None
    detail: str
    initiated_by: str
    created_at: datetime


class LinkTestStartResponse(BaseModel):
    test_id: str


class LinkTestListResponse(BaseModel):
    items: list[LinkTestOut]
    # Additive field, same items+total envelope pattern already used by
    # CascadeListResponse/UserListResponse (see schemas/cascades.py,
    # schemas/users.py) - contracts/rest/openapi.yaml's inline schema for this path
    # already declares both `items` and `total` (see the "Добавлено после реализации
    # Admin Client" note next to GET /cascades/{id}/tests in the contract), so this
    # one needs no deviation note of its own.
    total: int
