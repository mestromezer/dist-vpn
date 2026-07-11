"""One-time, TTL-bound enrollment tokens (TZ-5 §4.1, §6.2).

Not one of the entities literally listed in TZ-5 §3, but required to implement
`POST /nodes` (contracts/rest/openapi.yaml - "Сгенерировать одноразовый
enrollment-токен") and the gRPC `Enrollment` handshake (contracts/proto/agent_control.proto).
Only a hash of the token is stored, mirroring how the password hash is stored for
AdminAccount - the plaintext token is returned to the caller exactly once and never
persisted (TZ-5 §6.2 - "инвалидируется сразу после использования").
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow
from dist_vpn_platform.models.node import NodeRole


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    requested_role: Mapped[NodeRole] = mapped_column(sa_enum(NodeRole), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_by_admin_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("admin_accounts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node: Mapped["Node | None"] = relationship(back_populates="enrollment_token")
