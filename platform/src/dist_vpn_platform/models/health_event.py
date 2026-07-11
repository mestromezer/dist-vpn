"""HealthEvent - state-transition log for nodes/cascades (TZ-5 §3, §4.3).

The "what and where broke, and when" journal - e.g. "отвалилось в 14:32, из-за
таймаута наблюдателя на выходной ноде X". Kept as an explicit append-only log,
never overwritten, so history is queryable via `GET /cascades/{id}/events`.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, utcnow


class HealthEvent(Base):
    __tablename__ = "health_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cascade_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("cascades.id"), nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("nodes.id"), nullable=True)

    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
