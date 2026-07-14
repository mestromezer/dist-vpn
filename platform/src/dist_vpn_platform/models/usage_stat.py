"""UsageStat - byte counters only (TZ-5 §3, §4.4, §6.4).

Deliberately separate from HealthEvent/LinkTest (TZ-5 §4.3 - "API должен явно
разделять 'объём трафика'... и 'здоровье канала'... не смешивать в одной модели
данных"). Aggregated from agent_control.proto's TelemetryReport, which itself only
ever carries `scope`/`key`/uplink/downlink - never destination addresses or content.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, utcnow


class UsageStat(Base):
    __tablename__ = "usage_stats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # Mirrors TelemetryReport.UsageCounter.scope ("user" | "inbound" | "outbound").
    # user_id is populated when scope == "user" and the key (VLESS uuid/email) could be
    # resolved to a known User; raw_key always keeps the original reported key.
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_key: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.id"), nullable=False)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    uplink_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    downlink_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
