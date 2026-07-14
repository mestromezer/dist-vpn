"""One-time enrollment token generation/verification (TZ-5 §4.1, §6.2).

Tokens are high-entropy random strings (not user-chosen secrets), so a fast SHA-256
lookup hash is appropriate here (unlike AdminAccount passwords, which use the
deliberately-slow Argon2id in security/passwords.py) - the gRPC servicer needs to
look these up on every Enrollment message from potentially many concurrent Agents.
"""
from __future__ import annotations

import hashlib
import secrets


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
