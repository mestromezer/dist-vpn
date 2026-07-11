"""VPN end-user (TZ-5 §3, §4.4). Not to be confused with AdminAccount - this is a
person connecting through the fleet, identified to Agents/Xray by `uuid`, which never
changes across secret rotations (TZ-5 §4.4 - carried over principle from "M6")."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dist_vpn_platform.models.base import Base, new_uuid, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=new_uuid)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cascade_links: Mapped[list["UserCascade"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def cascade_ids(self) -> list[str]:
        return [link.cascade_id for link in self.cascade_links]


class UserCascade(Base):
    """Which cascades/presets a user has been granted access to (TZ-5 §4.4) - Platform
    fans this out to `AddUser`/`RemoveUser` commands on the affected Agents."""

    __tablename__ = "user_cascades"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    cascade_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cascades.id", ondelete="CASCADE"), primary_key=True
    )

    user: Mapped["User"] = relationship(back_populates="cascade_links")
