"""Test fixtures.

Production runs against PostgreSQL (see docs/TZ-5-platform.md §2). These tests run
against a throwaway file-based SQLite database via `aiosqlite` instead - there is no
Postgres available in every environment that might run this test suite, and the
schema uses only portable SQLAlchemy types (String/DateTime/JSON/Enum-as-VARCHAR,
see models/base.py `sa_enum`) specifically so this works. The Alembic baseline
migration itself IS verified against real PostgreSQL separately - see
platform/README.md "Testing" for how - tests here exercise the app/ORM layer, not
migration DDL.
"""
from __future__ import annotations

import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from dist_vpn_platform.app import create_app
from dist_vpn_platform.config import settings
from dist_vpn_platform.db import configure_database, get_engine
from dist_vpn_platform.models import Base
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.security import mtls_ca
from dist_vpn_platform.security.passwords import hash_password
from dist_vpn_platform.security.rate_limit import limiter
from dist_vpn_platform.services import link_tests


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[None]:
    db_path = Path(tempfile.gettempdir()) / f"dist_vpn_platform_test_{uuid.uuid4().hex}.sqlite3"
    configure_database(f"sqlite+aiosqlite:///{db_path}")
    engine = get_engine()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    # services/link_tests.py::start_link_test() fires a background task per test-
    # request that outlives the HTTP response - drain it before disposing the engine/
    # deleting the sqlite file below, or a still-running task can race the teardown
    # (see wait_for_background_tasks()'s docstring for the failure mode this avoids).
    await link_tests.wait_for_background_tasks()
    await engine.dispose()
    db_path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _mtls_ca_dir(tmp_path: Path) -> None:
    """Every test gets its own throwaway CA directory (same per-test isolation
    pattern as db_engine's per-test sqlite file, just synchronous since CA generation
    itself is) - real Enrollment.csr_pem handling (TZ-5 §6.2) now depends on a real CA
    being loadable via security/mtls_ca.py, and tests must not read/write a real
    shared `var/ca` on disk or leak CA material across test runs."""
    original = settings.mtls_ca_dir
    settings.mtls_ca_dir = str(tmp_path / "ca")
    mtls_ca.reset_cache_for_tests()
    yield
    settings.mtls_ca_dir = original
    mtls_ca.reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """slowapi's Limiter keeps in-memory counters across the whole test process -
    reset before every test so login-rate-limit tests don't leak into each other."""
    limiter.reset()


@pytest_asyncio.fixture
async def app(db_engine: None):
    # run_background_tasks=False - a periodic health-scheduler task has no place
    # running unattended across dozens of short-lived per-test event loops; see
    # tests/test_health_scheduler.py, which calls check_node_timeouts() directly.
    return create_app(run_startup_checks=False, run_background_tasks=False)


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver/api/v1") as ac:
        yield ac


@pytest_asyncio.fixture
async def seeded_admin(db_engine: None) -> dict:
    """A known admin account + plaintext password for login-flow tests."""
    from dist_vpn_platform.db import session_scope

    plaintext_password = "Correct-Horse-Battery-Staple-1"
    async with session_scope() as db:
        admin = AdminAccount(
            username="testadmin",
            password_hash=hash_password(plaintext_password),
            must_change_password=False,
        )
        db.add(admin)
        await db.flush()
        admin_id = admin.id

    return {"id": admin_id, "username": "testadmin", "password": plaintext_password}
