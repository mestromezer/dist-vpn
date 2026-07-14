"""Audit trail for device-limit enforcement (grpc_server/servicer.py::_handle_telemetry).

Kept as its own small table rather than folded into HealthEvent - HealthEvent is
Node/Cascade-status-scoped (up/degraded/down transitions), a device-limit kick is a
different kind of fact (User-scoped, not a status transition) that doesn't fit that
shape without overloading it.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, utcnow


class UserDeviceKickEvent(Base):
    __tablename__ = "user_device_kick_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.id"), nullable=False)

    online_ip_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False)

    kicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
