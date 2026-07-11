"""Async SQLAlchemy engine/session setup.

Production target is PostgreSQL (asyncpg). Tests use aiosqlite - see
platform/tests/conftest.py and the note in platform/README.md.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from dist_vpn_platform.config import settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


def configure_database(database_url: str) -> None:
    """Rebind the engine/session factory to a different URL (used by tests)."""
    global _engine, _session_factory
    settings.database_url = database_url
    _engine = create_async_engine(database_url, pool_pre_ping=True)
    _session_factory = async_sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped AsyncSession."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager for use outside of FastAPI's DI (gRPC servicer, scripts)."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database_connection() -> None:
    """Fail fast (TZ-5 §8 - 'чёткое сообщение об ошибке при недоступности БД') instead of
    hanging silently if Postgres is unreachable at startup."""
    from sqlalchemy import text

    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - re-raised with a clearer message below
        logger.error("database_unreachable", extra={"database_url_scheme": settings.database_url.split("://")[0]})
        raise RuntimeError(
            "Cannot connect to the database at startup. Check PLATFORM_DATABASE_URL and "
            "that PostgreSQL is running (see platform/docker-compose.yml)."
        ) from exc
