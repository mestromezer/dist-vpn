"""Application settings, loaded from environment variables / .env (pydantic-settings).

See docs/TZ-5-platform.md §6.3 for the security rationale behind the JWT/rate-limit defaults.
"""
from __future__ import annotations

import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PLATFORM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"

    # --- Database ---
    # Production: PostgreSQL via asyncpg (see TZ-5 §2). Tests override this with an
    # aiosqlite URL — see platform/tests/conftest.py and platform/README.md.
    database_url: str = "postgresql+asyncpg://platform:platform@localhost:5432/platform"

    # --- HTTP API ---
    http_host: str = "0.0.0.0"
    http_port: int = 8000

    # --- gRPC (Agent <-> Platform control plane) ---
    grpc_host: str = "0.0.0.0"
    # Strict mTLS - require_client_auth=True (grpc_server/server.py). Every
    # interaction except bootstrap Enrollment goes through this port.
    grpc_port: int = 50051
    # No client cert required or checked - see grpc_server/server.py's module
    # docstring for why this had to become a separate port rather than a mode on
    # grpc_port: found while validating a real two-VDS deployment that grpc's C-core
    # doesn't expose an *offered* client cert to the application at all once client
    # auth isn't required, so one port can't safely be both "cert required" and
    # "cert optional" - and Enrollment structurally cannot work without an optional
    # path (a brand-new Agent has no cert yet). Default is grpc_port + 1; Agent's
    # matching default (agent/src/dist_vpn_agent/config.py::
    # resolved_platform_enrollment_address) mirrors this convention - override both
    # together if you ever need them non-adjacent.
    grpc_enrollment_port: int = 50052
    # Max message size guard so one misbehaving/overloaded node cannot degrade the others
    # (TZ-5 §5 - "здоровое ограничение на размер сообщения/буферов").
    grpc_max_message_bytes: int = 4 * 1024 * 1024

    # --- JWT (TZ-5 §6.3) ---
    # MUST be overridden via PLATFORM_JWT_SECRET_KEY in any non-dev environment.
    jwt_secret_key: str = secrets.token_urlsafe(32)
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 20  # within the 15-30 min window required by TZ-5 §6.3
    jwt_refresh_ttl_days: int = 14

    # --- Login rate limiting (TZ-5 §6.3, slowapi) ---
    login_rate_limit_per_ip: str = "10/minute"
    # Per-account lockout, independent of the per-IP limit above (see AdminAccount
    # .failed_login_attempts/.locked_until in models/admin_account.py).
    login_max_failed_attempts: int = 5
    login_lockout_minutes: int = 15

    # --- Default admin bootstrap (TZ-5 §6.3 - "небезопасная конфигурация по умолчанию") ---
    default_admin_username: str = "admin"
    # If unset, a random password is generated at seed time and printed once - never
    # hardcoded, never committed. See seed.py.
    default_admin_password: str | None = None

    # --- Enrollment tokens (TZ-5 §4.1 / §6.2) ---
    enrollment_token_ttl_minutes: int = 30

    # --- Health scheduler (TZ-5 §4.3 - auto-detect DOWN by heartbeat timeout) ---
    # services/health_scheduler.py. 45s = 3x the Agent's own default
    # heartbeat_interval_seconds (15s, agent/src/dist_vpn_agent/config.py) - a "missed
    # 3 consecutive heartbeats" pattern, not itself specified by any TZ. If an Agent's
    # own heartbeat_interval_seconds is overridden away from the default, this should
    # be tuned to match - the two aren't coupled/validated against each other.
    node_heartbeat_timeout_seconds: int = 45
    # How often the scheduler re-checks for newly-stale nodes - independent of the
    # timeout above (a shorter interval just means faster detection within the
    # timeout window, not a shorter timeout itself).
    health_scheduler_interval_seconds: int = 10

    # --- Platform<->Platform peering (slice 9+, see platform/README.md "Девятый
    # слайс" / contracts/proto/platform_peering.proto's header comment) ---
    # None = single-Platform mode (default, unchanged behavior from before the
    # RU/foreign split - matches every existing deployment/test until provisioning
    # actually sets this, see provisioning/roles/platform_install). "entry" starts
    # services/link_negotiation.py (not yet built - slice 12) and dials peers as a
    # PlatformPeering client; "exit" starts grpc_server/peering_server.py and never
    # dials anyone (see contracts/proto/platform_peering.proto for why the dial
    # direction is fixed this way, not configurable).
    platform_segment: str | None = None
    # Separate port from grpc_port (Agent-facing, 50051) and grpc_enrollment_port
    # (50052) - a different mTLS/pinning trust anchor per listening socket is the
    # simplest correct option with grpc's ssl_server_credentials (no per-connection
    # SNI-based cert selection needed for exactly one possible peer). Only bound when
    # platform_segment == "exit" (grpc_server/peering_server.py).
    peering_grpc_port: int = 50053

    # --- mTLS CA (TZ-5 §6.2 - see security/mtls_ca.py for the full design note) ---
    # Persistent volume for the CA keypair/cert + server leaf keypair/cert. Local/non-
    # Docker dev: relative to the working directory. docker-compose.yml overrides this
    # to a named volume mounted at /var/lib/dist-vpn-platform/ca so it survives
    # container recreation.
    mtls_ca_dir: str = "var/ca"
    # Comma-separated hostnames/IPs for the server leaf cert's SAN - the Platform's own
    # gRPC server identity that Agents verify against the CA root. Must include
    # whatever hostname(s) Agents will actually dial plus localhost/127.0.0.1 for local
    # dev/tests.
    mtls_server_sans: str = "localhost,127.0.0.1"


settings = Settings()
