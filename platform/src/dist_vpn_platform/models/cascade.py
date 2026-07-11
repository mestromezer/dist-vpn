"""Cascade - an entry node bound to a pool of candidate exit nodes (TZ-5 §3, §4.2).

`status` is stored as an explicit column (not derived on the fly from scattered
counters) per TZ-5 §4.3's explicit requirement: "Хранить состояние каждой связки...
как явную сущность с текущим статусом".
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow


class CascadeStatus(str, enum.Enum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class Cascade(Base):
    __tablename__ = "cascades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    entry_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.id"), nullable=False)
    status: Mapped[CascadeStatus] = mapped_column(
        sa_enum(CascadeStatus), nullable=False, default=CascadeStatus.DOWN
    )
    # p1..p4 preset identifiers (see history referenced in TZ-5 §4.2) - a short list of
    # strings, portable as JSON across Postgres and SQLite.
    hop_a_presets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    exit_links: Mapped[list["CascadeExitNode"]] = relationship(
        back_populates="cascade", cascade="all, delete-orphan"
    )

    @property
    def exit_node_ids(self) -> list[str]:
        return [link.exit_node_id for link in self.exit_links]


class CascadeExitNode(Base):
    """Association: which exit nodes are candidates for a given cascade (the "Hop B
    candidate pool" from TZ-5 §4.2 - Xray balancer selects among these natively)."""

    __tablename__ = "cascade_exit_nodes"

    cascade_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cascades.id", ondelete="CASCADE"), primary_key=True
    )
    exit_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.id"), primary_key=True)

    cascade: Mapped["Cascade"] = relationship(back_populates="exit_links")
