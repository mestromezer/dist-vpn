"""RefreshToken - server-side record backing JWT refresh rotation + deny-list (TZ-5 §6.3).

Not one of the TZ-5 §3 entities, but required infrastructure for "refresh-токен с
ротацией и deny-list по jti при компрометации": the JWT itself only carries a `jti`
and `family_id`; this table is the source of truth for whether that jti is still
valid. Rotation: each successful /auth/refresh marks the presented row `revoked_at`
and inserts a new row with the same `family_id`. Replay of an already-rotated token
(reuse of a revoked jti) revokes the entire family - classic refresh-token-theft
detection, see the JWT Cheat Sheet referenced in TZ-5 §6.3.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, utcnow


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    jti: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, default=new_uuid)
    admin_id: Mapped[str] = mapped_column(String(36), ForeignKey("admin_accounts.id"), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_jti: Mapped[str | None] = mapped_column(String(36), nullable=True)
