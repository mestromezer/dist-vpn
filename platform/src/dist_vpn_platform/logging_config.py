"""Structured JSON logging.

TZ-5 §2/§8: JSON formatter, access/error separation, secrets never logged in full.
Mirrors the principle used in Agent (docs/TZ-4-agent.md §5.3): sensitive fields
(passwords, tokens, secrets, enrollment tokens) are always redacted, never emitted
even partially, regardless of log level.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger import jsonlogger

_REDACTED = "***REDACTED***"

# Field names that must never appear in logs in cleartext. Matched case-insensitively
# against `extra=` keys passed to the logger.
_SECRET_FIELD_NAMES = {
    "password",
    "totp_code",
    "totp_secret",
    "access_token",
    "refresh_token",
    "token",
    "enrollment_token",
    "mtls_client_cert_pem",
    "jwt_secret_key",
    "authorization",
}


class RedactingFilter(logging.Filter):
    """Redacts known-sensitive fields attached via `logging.Logger.*(..., extra={...})`."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__.keys()):
            if key.lower() in _SECRET_FIELD_NAMES and record.__dict__[key]:
                record.__dict__[key] = _REDACTED
        return True


def redact(value: Any) -> str:
    """Helper for call sites that must interpolate a secret into a message string.

    Prefer passing secrets via `extra=` (auto-redacted above); use this only when a
    secret must be referenced by a short fingerprint, e.g. `redact(token)` ->
    "***REDACTED***(len=43)" so the length is available for debugging without ever
    revealing content.
    """
    if value is None:
        return "None"
    return f"{_REDACTED}(len={len(str(value))})"


def configure_logging(log_level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )
    handler.setFormatter(formatter)
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level.upper())

    # Split access logs (uvicorn) from application/error logs at the logger-name level;
    # both go to stdout as structured JSON, downstream log shipping can route on `logger`.
    logging.getLogger("uvicorn.access").setLevel(log_level.upper())
    logging.getLogger("uvicorn.error").setLevel(log_level.upper())
