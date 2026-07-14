"""See contracts/rest/openapi.yaml components/schemas/UsageStat and path /usage.
Deliberately no destination-address/content fields (TZ-5 §6.4)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UsageStatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str | None = None
    node_id: str
    period_start: datetime
    uplink_bytes: int
    downlink_bytes: int
