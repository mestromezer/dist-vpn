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
        description=(
            "Platform's STRICT mTLS gRPC endpoint, host:port - used for every "
            "connection once this Agent has a persisted node identity (see "
            "platform_enrollment_address below for the one connection that happens "
            "before that)."
        ),
    )

    # Found while validating a real two-VDS deployment (not caught by mocked tests -
    # see TESTING.md): Platform's gRPC server used to require a client cert on this
    # same port unconditionally, which made bootstrap Enrollment structurally
    # impossible (a brand-new Agent has no cert yet - that's the entire point of the
    # one-time-token Enrollment RPC, control_client.py::run_session). Making the port
    # optionally-certed instead turned out to be unsafe: grpc's C-core (confirmed
    # against a real server, not just from docs) does not expose an *offered* client
    # cert's bytes to the server application at all when client auth isn't required -
    # so the server can't tell "no cert" from "a cert, just not verified" once it's
    # optional, and a single port cannot be both "require a valid cert" and "cert
    # optional" at the same time (that's a TLS handshake-time setting). Platform's
    # fix (dist_vpn_platform.grpc_server.server, same commit as this field) is a
    # SECOND listener with no client-cert requirement at all, used only for
    # Enrollment - default here (`platform_address`'s port + 1) matches the
    # convention documented on that server's `settings.grpc_enrollment_port` default;
    # override independently only if the two are ever deployed on non-adjacent ports.
    platform_enrollment_address: str | None = Field(
        default=None,
        description=(
            "Platform's unauthenticated (no client cert) enrollment-only gRPC "
            "endpoint, host:port. Defaults to platform_address with its port "
            "incremented by 1 if left unset - only override this if Platform's two "
            "ports aren't adjacent in your deployment."
        ),
    )

    @property
    def resolved_platform_enrollment_address(self) -> str:
        if self.platform_enrollment_address is not None:
            return self.platform_enrollment_address
        host, _, port = self.platform_address.rpartition(":")
        return f"{host}:{int(port) + 1}"

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

    # --- Persisted node identity (identity.py; see agent/README.md's "mTLS-
    # identity: bootstrap CSR, persisted reconnect" section for the full
    # design) ---
    # Directory the Agent persists its self-generated mTLS client keypair +
    # issued certificate + assigned node_id to, once EnrollmentAck returns a
    # signed certificate (see identity.py). Checked on every ``run_session()``
    # (fresh process start *or* in-process reconnect) before falling back to
    # bootstrap-enrollment with ``enrollment_token``. Default matches this
    # Agent's actual deployment target (TZ-4 SS5.3: a Linux container/host,
    # run as a dedicated non-root user) -- ``/var/lib/<service-name>`` is the
    # conventional Linux location for a service's own persistent local state,
    # analogous to e.g. ``/var/lib/docker`` or ``/var/lib/postgresql``. Not
    # ``/etc`` (that's for operator-supplied config, e.g.
    # ``xray_config_host_path`` below) and not ``/tmp`` (must survive
    # reboots). Must be writable by the Agent's process user; the directory
    # itself is created on first use if missing (see identity.py
    # persist_identity).
    state_dir: Path = Field(
        default=Path("/var/lib/dist-vpn-agent"),
        description="Directory for persisted node identity (mTLS client key+cert+node_id, see identity.py).",
    )

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

    # --- Local Xray-core control API (xray_client.py) ---
    # Loopback address of the Xray-core container's own gRPC API
    # (HandlerService/RoutingService/StatsService, TZ-4 SS3.2) -- NOT the
    # Agent<->Platform channel above. This is a purely local, same-host
    # (or same Docker network namespace) connection; it is never exposed
    # externally, so it does not carry the mTLS requirements that apply to
    # platform_address (see xray_client.py for why plaintext loopback here
    # is not the same risk as insecure_dev_mode).
    xray_api_address: str = Field(
        default="127.0.0.1:10085",
        description="host:port of the local Xray-core gRPC API (loopback only).",
    )

    # --- Local Xray-core Docker container (docker_manager.py) ---
    xray_docker_image: str = Field(
        default="ghcr.io/xtls/xray-core:latest",
        description="Image (with tag) to run/create the local Xray-core container from.",
    )
    xray_container_name: str = Field(
        default="dist-vpn-xray-core",
        description="Name of the Xray-core container this Agent manages (only this container is ever touched, TZ-4 SS5.4).",
    )
    xray_config_host_path: Path = Field(
        default=Path("/etc/dist-vpn/xray-config.json"),
        description="Host path mounted read-only into the container as /etc/xray/config.json.",
    )
    # Client/backhaul-facing port to publish (0.0.0.0) on the Xray-core container at
    # creation time - found missing while validating a real two-VDS deployment: unlike
    # xray_api_address above (loopback, published at creation, fine), the Hop A/Hop B
    # inbounds this Agent adds *after* the container is already running via AddInbound
    # (control_client.py -> xray_client.py) are never published to the host at all
    # unless a `-p` mapping for that port existed when the container was *created* -
    # Docker cannot add port publishing to an already-running container. Defaults to
    # 443, matching the Platform side's current single-port convention
    # (platform/src/dist_vpn_platform/services/xray_templates.py::HOP_LISTEN_PORT) -
    # both must agree, since neither side is aware of the other's port choice at
    # runtime (see platform/README.md TODO on making the preset port configurable).
    xray_public_port: int = Field(
        default=443,
        description="Host port (0.0.0.0) published on the Xray-core container for client/backhaul VLESS traffic.",
    )

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
