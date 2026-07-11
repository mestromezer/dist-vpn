"""AdminAccount - administrator credentials (TZ-5 §3, §6.3).

Security-relevant columns beyond the bare TZ-5 §3 description, each tied to a
specific §6.3 requirement:
  - password_hash: Argon2id (never plaintext/legacy hash) - see security/passwords.py.
  - must_change_password: enforces "дефолтный пароль... требует немедленной смены".
  - totp_secret/totp_enabled: MFA for the (likely single) full-access account.
  - failed_login_attempts/locked_until: per-account lockout, independent of the
    per-IP slowapi limit on the route (TZ-5 §6.3 - "лимит... на IP и на логин отдельно").
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, utcnow


class AdminAccount(Base):
    __tablename__ = "admin_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="admin")

    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
