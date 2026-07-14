"""slowapi rate limiter (TZ-5 §6.3 - "лимит на IP... на /auth/login").

Keyed by client IP by default (slowapi's `get_remote_address`). See
security/auth_service.py for the complementary per-account lockout.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
