from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def new_uuid() -> str:
    """String UUIDs (rather than a Postgres-only UUID column type) so the same models
    work against both PostgreSQL (production) and SQLite (tests) - see TZ-5 §2 stack
    note and platform/README.md for why tests run on aiosqlite."""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware_utc(value: datetime) -> datetime:
    """SQLite (used in tests, see platform/tests/conftest.py) does not preserve
    timezone-awareness across a round-trip even for `DateTime(timezone=True)` columns -
    it comes back naive. PostgreSQL (production) does preserve it. Normalize to
    timezone-aware UTC before comparing against `utcnow()` so the same code works
    against both backends."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def sa_enum(enum_cls: type[enum.Enum], length: int = 32) -> SAEnum:
    """VARCHAR + CHECK constraint (not a native Postgres ENUM type) so the same schema
    works on both PostgreSQL and the SQLite test database, and so adding a new member
    later is a plain data migration rather than an `ALTER TYPE` dance."""
    return SAEnum(enum_cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=length)
