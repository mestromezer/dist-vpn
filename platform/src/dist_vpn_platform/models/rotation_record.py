"""RotationRecord - history of secret rotations (TZ-5 §3, §4.5).

`kind` mirrors agent_control.proto RotationKind (minus UNSPECIFIED). Two-phase
rotation status (pending/succeeded/failed/rolled_back) is tracked explicitly per
TZ-5 §4.5's "two-phase model... new alongside old, revoke old after grace period".
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow


class RotationKind(str, enum.Enum):
    HOPA_PRESET = "hopa_preset"
    HOPB_KEY = "hopb_key"
    P4_CERT = "p4_cert"


class RotationStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class RotationRecord(Base):
    __tablename__ = "rotation_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    rotation_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=new_uuid)
    kind: Mapped[RotationKind] = mapped_column(sa_enum(RotationKind), nullable=False)
    status: Mapped[RotationStatus] = mapped_column(
        sa_enum(RotationStatus), nullable=False, default=RotationStatus.PENDING
    )

    # 'admin:<username>' for manual, 'schedule' for ARQ-triggered (TZ-5 §4.5).
    initiated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
