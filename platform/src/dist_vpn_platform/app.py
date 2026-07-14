"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from dist_vpn_platform.api.router import api_router
from dist_vpn_platform.config import settings
from dist_vpn_platform.db import check_database_connection
from dist_vpn_platform.logging_config import configure_logging
from dist_vpn_platform.security.rate_limit import limiter
from dist_vpn_platform.services.health_scheduler import run_health_scheduler_forever

logger = logging.getLogger(__name__)


def create_app(*, run_startup_checks: bool = True, run_background_tasks: bool = True) -> FastAPI:
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if run_startup_checks:
            # TZ-5 §8: fail fast and loud instead of hanging silently if Postgres is down.
            await check_database_connection()
            logger.info("startup_checks_passed")

        scheduler_task: asyncio.Task | None = None
        if run_background_tasks:
            # services/health_scheduler.py - separate opt-out from run_startup_checks
            # above: tests/conftest.py disables both for the fast REST-level test
            # suite (a periodic background task has no place running unattended
            # across dozens of short-lived per-test event loops), but
            # test_health_scheduler.py calls check_node_timeouts() directly rather
            # than going through this lifespan at all either way.
            scheduler_task = asyncio.create_task(run_health_scheduler_forever())
            logger.info("health_scheduler_started")

        yield

        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task

    app = FastAPI(
        title="dist-vpn Platform API",
        version="0.1.0",
        description=(
            "REST API for managing the node pool, cascades, users and monitoring. "
            "See contracts/rest/openapi.yaml (source of truth) and docs/TZ-5-platform.md."
        ),
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.include_router(api_router)

    return app
