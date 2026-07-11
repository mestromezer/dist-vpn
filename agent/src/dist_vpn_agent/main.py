"""Process entrypoint: run the control-plane client against Platform.

This slice only wires up the Agent<->Platform gRPC channel (enrollment,
heartbeat, ping, command-echo). It does not manage Xray or Docker yet --
see agent/README.md for scope.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from pydantic import ValidationError

from dist_vpn_agent.config import AgentSettings
from dist_vpn_agent.control_client import ControlClient
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

    client = ControlClient(settings)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        logger.info("shutting down (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
