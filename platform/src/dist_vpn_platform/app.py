"""FastAPI application factory."""
from __future__ import annotations

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

logger = logging.getLogger(__name__)


def create_app(*, run_startup_checks: bool = True) -> FastAPI:
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if run_startup_checks:
            # TZ-5 §8: fail fast and loud instead of hanging silently if Postgres is down.
            await check_database_connection()
            logger.info("startup_checks_passed")
        yield

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
