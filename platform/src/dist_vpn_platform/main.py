"""Process entrypoint: runs the FastAPI/uvicorn HTTP server and the gRPC AgentControl
server concurrently in one process (single `docker compose up`-able unit, TZ-5 §8).

    python -m dist_vpn_platform.main
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from dist_vpn_platform.app import create_app
from dist_vpn_platform.config import settings
from dist_vpn_platform.grpc_server.server import serve as serve_grpc
from dist_vpn_platform.logging_config import configure_logging

logger = logging.getLogger(__name__)


async def _run() -> None:
    configure_logging(settings.log_level)
    app = create_app()

    uvicorn_config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,  # keep our JSON logging config instead of uvicorn's default
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)

    logger.info(
        "platform_process_starting",
        extra={"http_port": settings.http_port, "grpc_port": settings.grpc_port, "env": settings.env},
    )
    await asyncio.gather(uvicorn_server.serve(), serve_grpc())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
