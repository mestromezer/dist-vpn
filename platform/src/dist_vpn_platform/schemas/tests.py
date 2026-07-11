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
