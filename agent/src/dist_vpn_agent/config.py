"""Agent configuration.

Loaded via pydantic-settings from environment variables (prefix ``AGENT_``)
and/or a ``.env`` file. See ``agent/README.md`` for the full list of
supported environment variables and example values.

Security note (TZ-4 SS5.3): nothing in this module should ever be logged in
full. ``enrollment_token`` is a ``SecretStr`` so ``repr()``/``str()`` on the
settings object never leaks it; callers that need to log something about a
secret must go through :func:`mask_secret` below instead of accessing
``.get_secret_value()`` directly.
"""

from __future__ import annotations

import enum
import socket
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class NodeRole(str, enum.Enum):
    """Mirrors ``distvpn.control.v1.NodeRole`` in agent_control.proto."""

    UNSPECIFIED = "unspecified"
    ENTRY = "entry"  # RF-facing entry node (Hop A)
    EXIT = "exit"  # foreign-exit node (Hop B)


class AgentSettings(BaseSettings):
    """All runtime configuration for the Agent process.

    Every field is overridable via an ``AGENT_<FIELD_NAME>`` environment
    variable (case-insensitive), e.g. ``AGENT_PLATFORM_ADDRESS``,
    ``AGENT_HEARTBEAT_INTERVAL_SECONDS``. A ``.env`` file in the working
    directory is also picked up if present (never commit one -- see the
    repo's root .gitignore).
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Platform connection ---
    platform_address: str = Field(
        default="localhost:50051",
        description="Platform gRPC endpoint, host:port. Agent always dials out to this address.",
    )

    # --- Enrollment (bootstrap only; single-use token, see TZ-4 SS5.2) ---
    enrollment_token: SecretStr = Field(
        default=SecretStr(""),
        description="One-time, TTL-bound enrollment token. Useless after the first successful enrollment.",
    )
    hostname: str = Field(
        default_factory=socket.gethostname,
        description="Reported to Platform as this node's hostname.",
    )
    agent_version: str = Field(default="0.1.0")
    requested_role: NodeRole = Field(default=NodeRole.UNSPECIFIED)

    # --- Heartbeat / session ---
    heartbeat_interval_seconds: float = Field(
        default=15.0,
        gt=0,
        description="TZ-4 SS3.1 default is 15s. Configurable.",
    )
    reconnect_backoff_initial_seconds: float = Field(default=1.0, gt=0)
    reconnect_backoff_max_seconds: float = Field(default=30.0, gt=0)

    # --- mTLS (TZ-4 SS5.2) ---
    # mtls_ca_cert_path is required (unless insecure_dev_mode) so the Agent can
    # always authenticate Platform's server certificate. mtls_client_cert_path /
    # mtls_client_key_path are optional: on the very first boot the Agent has no
    # node-specific client certificate yet (that's the whole point of the
    # one-time enrollment token -- see TZ-4 SS3.1/SS5.2), so that first call is
    # plain server-authenticated TLS with the token carried in the Enrollment
    # message as the application-layer credential. Once a client cert has been
    # issued (persisting the one returned in EnrollmentAck is a follow-up slice,
    # see agent/README.md), operators should set both client_cert/client_key so
    # reconnects use full mTLS as TZ-4 intends.
    mtls_client_cert_path: Path | None = Field(default=None)
    mtls_client_key_path: Path | None = Field(default=None)
    mtls_ca_cert_path: Path | None = Field(default=None)

    # Explicit, opt-in-only escape hatch for local dev. Never the default;
    # the client logs a loud warning whenever this is true (see
    # control_client.py). Production deployments must leave this false and
    # supply real mTLS material instead.
    insecure_dev_mode: bool = Field(default=False)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    @model_validator(mode="after")
    def _check_tls_material_present_unless_insecure(self) -> "AgentSettings":
        if self.insecure_dev_mode:
            return self

        if self.mtls_ca_cert_path is None:
            raise ValueError(
                "mtls_ca_cert_path is required unless insecure_dev_mode is set -- "
                "the Agent must always be able to authenticate Platform's server "
                "certificate (TZ-4 SS5.2). Setting insecure_dev_mode=true is unsafe "
                "for production."
            )

        client_cert_set = self.mtls_client_cert_path is not None
        client_key_set = self.mtls_client_key_path is not None
        if client_cert_set != client_key_set:
            raise ValueError(
                "mtls_client_cert_path and mtls_client_key_path must both be set "
                "or both be left unset (a client cert without its key, or vice "
                "versa, cannot form a valid TLS client identity)."
            )
        return self


def mask_secret(value: str, keep: int = 4) -> str:
    """Redact a secret for logging: keep a short prefix, mask the rest.

    Used any time we want to log *that* a token/cert/key was present and
    roughly which one, without ever writing the actual secret material to
    logs (TZ-4 SS5.3 / OWASP A09).
    """
    if not value:
        return "<empty>"
    prefix = value[:keep]
    return f"{prefix}{'*' * max(len(value) - keep, 3)}"
