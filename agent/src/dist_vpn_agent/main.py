"""Process entrypoint: run the control-plane client against Platform.

Also ensures the local Xray-core container is up before starting the
control-plane loop (docker_manager.py, TZ-4 SS5.4) -- deliberately
independent of whether Platform is reachable: existing users may already be
routed through a running Xray-core even during a Platform outage (TZ-4 SS4:
"Agent должен переживать кратковременную потерю связи с Platform ... без
падения локального Xray"), so this must not be gated on enrollment/session
success.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from pydantic import ValidationError

from dist_vpn_agent.config import AgentSettings
from dist_vpn_agent.control_client import ControlClient
from dist_vpn_agent.docker_manager import DockerManager, DockerManagerError
from dist_vpn_agent.logging_setup import configure_logging

logger = logging.getLogger(__name__)


def main() -> None:
    # Load settings before logging is configured so a bad .env / missing
    # mTLS paths fails fast with a clear message (TZ-4 SS7: "explicit
    # self-diagnostics instead of silent hang").
    try:
        settings = AgentSettings()
    except ValidationError as exc:
        print(f"Agent configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    configure_logging(settings.log_level)
    logger.info(
        "starting dist-vpn-agent control client: platform=%s role=%s heartbeat_interval=%ss",
        settings.platform_address,
        settings.requested_role.value,
        settings.heartbeat_interval_seconds,
    )

    docker_manager = DockerManager(settings)
    try:
        health = docker_manager.ensure_running()
        logger.info(
            "local Xray-core container %s: %s", settings.xray_container_name, health.status
        )
    except DockerManagerError:
        # Explicit, loud self-diagnostic (TZ-4 SS7) rather than a silent
        # hang -- but not fatal: the control-plane session should still
        # come up and honestly report xray_container_running=False via the
        # heartbeat rather than the whole process refusing to start.
        logger.exception(
            "could not ensure the local Xray-core container is running; continuing "
            "anyway so Platform can at least see this node and its real (unhealthy) status"
        )

    client = ControlClient(settings, docker_manager=docker_manager)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        logger.info("shutting down (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
