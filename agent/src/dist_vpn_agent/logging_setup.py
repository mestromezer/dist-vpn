"""Structured JSON logging setup.

TZ-4 SS5.3 / SS7 requirement: logs must be structured (for aggregation on
Platform) and must never contain secret material in full. This module only
configures *formatting*; callers are responsible for never passing secret
values directly into log messages -- use ``config.mask_secret`` for that.
"""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger to emit single-line JSON records to stdout.

    Safe to call more than once (e.g. in tests) -- it replaces any handlers
    it previously installed rather than stacking duplicates.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    for existing in list(root.handlers):
        if getattr(existing, "_dist_vpn_agent_handler", False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(jsonlogger.JsonFormatter(_LOG_FORMAT))
    handler._dist_vpn_agent_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
