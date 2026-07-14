"""Login flow tests (TZ-5 §6.3): success, failure, rate limiting, refresh rotation."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_login_success_returns_token_pair(client: AsyncClient, seeded_admin: dict) -> None:
    response = await client.post(
        "/auth/login",
        json={"username": seeded_admin["username"], "password": seeded_admin["password"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] > 0
    assert body["must_change_password"] is False


@pytest.mark.asyncio
async def test_login_wrong_password_rejected(client: AsyncClient, seeded_admin: dict) -> None:
    response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": "definitely-wrong"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_user_rejected(client: AsyncClient, db_engine: None) -> None:
    response = await client.post("/auth/login", json={"username": "nobody", "password": "whatever"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_rate_limited_after_threshold(client: AsyncClient, seeded_admin: dict) -> None:
    # Default limit is 10/minute (PLATFORM_LOGIN_RATE_LIMIT_PER_IP) - the 11th request
    # from the same client within the window must be rejected with 429 regardless of
    # the credentials supplied (TZ-5 §6.3 - brute-force/credential-stuffing defense).
    last_response = None
    for _ in range(11):
        last_response = await client.post(
            "/auth/login", json={"username": seeded_admin["username"], "password": "wrong-on-purpose"}
        )
    assert last_response.status_code == 429


@pytest.mark.asyncio
async def test_account_locks_after_repeated_failures(client: AsyncClient, seeded_admin: dict) -> None:
    # Per-account lockout is independent of the per-IP slowapi limit above (TZ-5 §6.3 -
    # "лимит на IP и на логин отдельно"). Default threshold is 5 failed attempts.
    for _ in range(5):
        response = await client.post(
            "/auth/login", json={"username": seeded_admin["username"], "password": "wrong-on-purpose"}
        )
        assert response.status_code == 401

    # Even the *correct* password is now rejected because the account is locked.
    response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert response.status_code == 401
    assert "locked" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_rotates_and_old_token_is_rejected_on_reuse(
    client: AsyncClient, seeded_admin: dict
) -> None:
    login_response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    original_refresh_token = login_response.json()["refresh_token"]

    refresh_response = await client.post("/auth/refresh", json={"refresh_token": original_refresh_token})
    assert refresh_response.status_code == 200
    rotated_refresh_token = refresh_response.json()["refresh_token"]
    assert rotated_refresh_token != original_refresh_token

    # Reusing the now-rotated-away original token must fail (deny-list / theft detection).
    replay_response = await client.post("/auth/refresh", json={"refresh_token": original_refresh_token})
    assert replay_response.status_code == 401

    # And per the theft-detection design, the whole family (including the token that
    # replaced it) is revoked too - so even the legitimately-rotated token now fails.
    followup_response = await client.post("/auth/refresh", json={"refresh_token": rotated_refresh_token})
    assert followup_response.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(client: AsyncClient, seeded_admin: dict) -> None:
    login_response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    tokens = login_response.json()

    logout_response = await client.post(
        "/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert logout_response.status_code == 204

    refresh_after_logout = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh_after_logout.status_code == 401


@pytest.mark.asyncio
async def test_logout_without_body_is_accepted(client: AsyncClient, seeded_admin: dict) -> None:
    # contracts/rest/openapi.yaml's /auth/logout has no requestBody at all (v0.1 draft
    # gap); this slice makes the refresh_token body optional rather than required -
    # a bare POST with only the bearer token must still succeed (see
    # api/routes/auth.py, schemas/auth.py LogoutRequest).
    login_response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    access_token = login_response.json()["access_token"]

    logout_response = await client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert logout_response.status_code == 204


@pytest.mark.asyncio
async def test_change_password_success_clears_must_change_password_and_revokes_sessions(
    client: AsyncClient, seeded_admin: dict
) -> None:
    login_response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    tokens = login_response.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    change_response = await client.post(
        "/auth/change-password",
        json={"current_password": seeded_admin["password"], "new_password": "New-Correct-Horse-42"},
        headers=headers,
    )
    assert change_response.status_code == 204

    # The old refresh token (issued before the password change) must now be revoked -
    # a credential change ends previously-issued sessions (see
    # security/auth_service.py::change_password).
    stale_refresh = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert stale_refresh.status_code == 401

    # Old password no longer works; new password does, and must_change_password is
    # now False (it was already False for seeded_admin, but this proves the write path).
    old_password_login = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert old_password_login.status_code == 401

    new_password_login = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": "New-Correct-Horse-42"}
    )
    assert new_password_login.status_code == 200
    assert new_password_login.json()["must_change_password"] is False


@pytest.mark.asyncio
async def test_change_password_wrong_current_password_is_401(client: AsyncClient, seeded_admin: dict) -> None:
    login_response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = await client.post(
        "/auth/change-password",
        json={"current_password": "totally-wrong", "new_password": "New-Correct-Horse-42"},
        headers=headers,
    )
    assert response.status_code == 401

    # The password must be unchanged - the original still logs in.
    still_works = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert still_works.status_code == 200


@pytest.mark.asyncio
async def test_change_password_requires_authentication(client: AsyncClient, db_engine: None) -> None:
    response = await client.post(
        "/auth/change-password", json={"current_password": "x", "new_password": "y"}
    )
    assert response.status_code == 401
