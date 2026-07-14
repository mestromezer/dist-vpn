"""Time series of node resource usage vs. concurrent user/device counts.

Node.cpu_percent/mem_percent/disk_percent (models/node.py) only ever hold the most
recent Heartbeat's values - every earlier sample is overwritten, so there was no way
to answer "how did resource usage trend as the user count on this node grew" at all.
One row is written per incoming Heartbeat (grpc_server/servicer.py::_handle_heartbeat)
- see platform/README.md for the explicit, undealt-with row-growth caveat this implies
at the default 15s heartbeat interval.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid


class NodeResourceSample(Base):
    __tablename__ = "node_resource_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False)
    mem_percent: Mapped[float] = mapped_column(Float, nullable=False)
    disk_percent: Mapped[float] = mapped_column(Float, nullable=False)

    # "Provisioned" = has access to a cascade whose entry node is this node (a DB
    # count, not derived from telemetry) - distinct from "online", which is whatever
    # this node's most recent TelemetryReport.user_online actually reported as
    # currently connected right now. Both are useful for different capacity-planning
    # questions ("how many users am I entitled to serve" vs "how many are actually
    # hitting me at once").
    provisioned_user_count: Mapped[int] = mapped_column(Integer, nullable=False)
    online_user_count: Mapped[int] = mapped_column(Integer, nullable=False)
    online_device_count: Mapped[int] = mapped_column(Integer, nullable=False)
