"""Argon2id password hashing (TZ-5 §6.3, OWASP Password Storage Cheat Sheet).

Parameters follow the OWASP baseline referenced in TZ-5 §2: >=19 MiB memory, 2
iterations, 1 degree of parallelism (argon2-cffi's PasswordHasher defaults already
meet/exceed this - time_cost=2 ... but we pin them explicitly rather than relying on
library defaults so the actual parameters can't silently drift on a library upgrade).
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,  # KiB -> 19 MiB
    parallelism=1,
)


def hash_password(plain_password: str) -> str:
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, plain_password)
    except VerifyMismatchError:
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """Call after a successful verify; if True, re-hash with current params and save."""
    return _hasher.check_needs_rehash(password_hash)
