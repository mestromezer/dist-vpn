"""See contracts/rest/openapi.yaml components/schemas/HealthEvent and path
/cascades/{cascade_id}/events."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class HealthEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cascade_id: str | None = None
    node_id: str | None = None
    from_status: str
    to_status: str
    reason: str
    occurred_at: datetime
