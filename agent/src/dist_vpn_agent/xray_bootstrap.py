"""Generates the *base* Xray-core config file (``xray_config_host_path`` in
config.py) that ``docker_manager.py`` mounts read-only into the Xray-core
container it manages.

--- Why this module exists (a real, previously-missing prerequisite) ---

``docker_manager.py`` has always mounted this file, but nothing anywhere in
this codebase (Agent, ``provisioning/``, ``agent/Dockerfile``) ever generated
its *content* - the only place this exact shape existed was a test fixture
inside ``tests/test_xray_client.py`` (``_real_xray_config``), used purely to
stand up a throwaway container for that test's own assertions, never shipped
anywhere. Without this file, the Xray-core container has no ``api`` inbound
at all, so ``xray_client.py``'s entire HandlerService/RoutingService/
StatsService integration - everything this Agent does after the container
starts - would have nothing to connect to. Found while wiring
``StatsService.GetUsersStats`` for per-user online-device tracking, but it
blocks the *already-shipped* traffic telemetry too (``TelemetryReport``
needs ``statsUserUplink``/``statsUserDownlink``, neither of which had a
``policy`` block enabling them anywhere either) - this was never just a gap
in the new feature, it's a gap in the base of the whole Xray-management
story.

--- What this generates ---

The same proven shape as the test fixture (a ``dokodemo-door`` "api" inbound
on loopback, routed to a ``freedom`` "direct" outbound, per Xray-core's own
documented API-access pattern), plus what real per-user telemetry needs and
the test fixture didn't have: a top-level ``stats: {}`` object (required for
Xray to track counters at all - "internal statistics enabled as long as the
StatsObject item exists", xtls.github.io/en/config/stats.html) and a
``policy.levels["0"]`` with ``statsUserUplink``/``statsUserDownlink``/
``statsUserOnline`` all ``true`` (xtls.github.io/en/config/policy.html;
confirmed there that ``statsUserOnline`` does not require ``log.access`` to
be enabled - this project keeps access logging off for privacy, TZ-4 SS3.5).
Level ``"0"`` specifically because every VLESS client this Agent adds
(``xray_client.py``'s ``AddUser`` handling) omits an explicit ``level``,
which Xray defaults to ``0``.

--- Idempotency: never overwrites an existing file ---

If ``xray_config_host_path`` already exists, this is a no-op - an operator
who has hand-tuned the file (extra inbounds provisioned some other way,
custom ``log``/``policy`` tuning) must not have it silently clobbered on the
next Agent restart. This only ever *creates* the file on a fresh node where
nothing has run yet.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from dist_vpn_agent.config import AgentSettings

logger = logging.getLogger(__name__)


def ensure_base_config(settings: AgentSettings) -> None:
    path = settings.xray_config_host_path
    if path.exists():
        logger.debug("Xray base config %s already exists, leaving it alone", path)
        return

    _, _, port_str = settings.xray_api_address.rpartition(":")
    if not port_str:
        raise ValueError(f"xray_api_address must be host:port, got {settings.xray_api_address!r}")
    port = int(port_str)

    config = {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["HandlerService", "RoutingService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                    "statsUserOnline": True,
                }
            }
        },
        "inbounds": [
            {
                # 0.0.0.0 *inside the container's own network namespace* - not a
                # contradiction of "loopback-only" (docker_manager.py's own
                # reasoning for this same port): Docker's host-side port publish
                # (docker_manager.py's `ports={...}`) NATs from the host's
                # 127.0.0.1 through the container's external (eth0) interface,
                # not its internal loopback - if Xray bound to 127.0.0.1 *inside*
                # the container, that NAT could never reach it at all. The actual
                # "never exposed beyond loopback" guarantee is enforced entirely
                # by docker_manager.py's host-side publish target, not by this
                # in-container bind address (same shape already proven working
                # in tests/test_xray_client.py's real-Docker integration tier).
                "listen": "0.0.0.0",
                "port": port,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
                "tag": "api",
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    logger.info("generated base Xray config at %s (api port %d)", path, port)
