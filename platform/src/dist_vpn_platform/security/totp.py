"""TOTP verification (TZ-5 §6.3 - MFA for the admin account).

Only verification is implemented in this slice. Provisioning a new secret needs a
QR-code enrollment step that is not part of contracts/rest/openapi.yaml v0.1 - see
platform/README.md "Scope of this slice / TODO". `AdminAccount.totp_enabled`
defaults to False for the seeded account until that flow exists.
"""
from __future__ import annotations

import pyotp


def generate_secret() -> str:
    return pyotp.random_base32()


def verify_code(secret: str, code: str) -> bool:
    if not code:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)
