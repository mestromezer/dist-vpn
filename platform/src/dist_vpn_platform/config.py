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
    grpc_port: int = 50051
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


settings = Settings()
