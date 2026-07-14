"""Seed the default AdminAccount (TZ-5 §6.3 - "Небезопасная конфигурация по умолчанию":
the default admin password is generated randomly at first deploy, printed exactly
once, and must be changed immediately - never hardcoded, never committed).

Usage:
    python -m dist_vpn_platform.seed
"""
from __future__ import annotations

import asyncio
import logging
import secrets

from sqlalchemy import select

from dist_vpn_platform.config import settings
from dist_vpn_platform.db import session_scope
from dist_vpn_platform.logging_config import configure_logging
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.security.passwords import hash_password

logger = logging.getLogger(__name__)


async def seed_default_admin() -> None:
    async with session_scope() as db:
        result = await db.execute(
            select(AdminAccount).where(AdminAccount.username == settings.default_admin_username)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            logger.info("default_admin_already_exists", extra={"username": settings.default_admin_username})
            return

        password = settings.default_admin_password or secrets.token_urlsafe(18)
        admin = AdminAccount(
            username=settings.default_admin_username,
            password_hash=hash_password(password),
            role="admin",
            must_change_password=True,
        )
        db.add(admin)
        await db.flush()

        if settings.default_admin_password is None:
            # Printed exactly once, deliberately outside the structured JSON logger so
            # it isn't accidentally captured by log shipping - an operator must copy it
            # from the console at seed time.
            print("=" * 72)
            print(f"Default admin created: username={settings.default_admin_username!r}")
            print(f"Generated one-time password: {password}")
            print("This password is NOT stored anywhere else and will NOT be shown again.")
            print("You must change it on first login (must_change_password=True).")
            print("=" * 72)
        logger.info("default_admin_seeded", extra={"username": settings.default_admin_username})


def main() -> None:
    configure_logging(settings.log_level)
    asyncio.run(seed_default_admin())


if __name__ == "__main__":
    main()
