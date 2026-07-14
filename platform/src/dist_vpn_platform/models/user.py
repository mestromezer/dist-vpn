"""VPN end-user (TZ-5 §3, §4.4). Not to be confused with AdminAccount - this is a
person connecting through the fleet, identified to Agents/Xray by `uuid`, which never
changes across secret rotations (TZ-5 §4.4 - carried over principle from "M6")."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dist_vpn_platform.models.base import Base, new_uuid, utcnow

# Default device cap for a newly-created User when the request doesn't specify one -
# a deliberate, documented judgment call (not a value from any TZ), see
# schemas/users.py UserCreateRequest.max_devices and platform/README.md.
DEFAULT_MAX_DEVICES = 3


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=new_uuid)

    # NULL = unlimited (explicit admin opt-out, not "unset"/"unknown" - see
    # schemas/users.py). Enforced per-node from TelemetryReport.user_online
    # (grpc_server/servicer.py::_handle_telemetry) - see that method's docstring for
    # the "per-node, not cross-node aggregate" scope boundary.
    #
    # Deliberately NO `default=DEFAULT_MAX_DEVICES` here (confirmed by a real,
    # reproduced bug during this slice, not a style preference): SQLAlchemy's
    # column-level Python-side `default=` fires whenever the value being flushed is
    # `None`, and cannot distinguish "attribute never touched" from "explicitly
    # assigned None" - `User(max_devices=None)` silently became `3` at flush time
    # with a column-level default present, which would have made "explicitly
    # unlimited" via PATCH .../users/{id} {"max_devices": null} impossible to
    # express. The "3 if the request omitted the field entirely" behavior lives
    # exclusively at the Pydantic layer instead (schemas/users.py
    # UserCreateRequest.max_devices = DEFAULT_MAX_DEVICES), which correctly
    # distinguishes "key absent in the JSON body" from "key present with value
    # null" - api/routes/users.py always passes an explicit value through to this
    # column, so it firing this way at the DB layer was never actually needed and
    # only would have caused bugs.
    max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cached from the most recent TelemetryReport.user_online this user appeared in -
    # lets GET /users/{id} show a live-ish device count without a synchronous round
    # trip to any Agent's Xray-core. "Cached", not authoritative: reflects whatever a
    # node last reported, may be stale if that node has been unreachable for a while.
    last_online_device_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
