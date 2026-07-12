"""GET/POST /users, GET/DELETE /users/{user_id} (contracts/rest/openapi.yaml) - real
CRUD backed by a persisted User row and live AddUser/RemoveUser dispatch to the
cascade's entry-node Agent (see tests/grpc_test_helpers.py::FakeAgent)."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from dist_vpn_platform.models.node import NodeRole
from tests.grpc_test_helpers import FakeAgent


async def _login(client: AsyncClient, seeded_admin: dict) -> str:
    response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_cascade(client: AsyncClient, token: str, entry_node_id: str) -> str:
    response = await client.post(
        "/cascades",
        json={"entry_node_id": entry_node_id, "exit_node_ids": []},
        headers=_auth_headers(token),
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.asyncio
async def test_list_users_requires_authentication(client: AsyncClient, db_engine: None) -> None:
    response = await client.get("/users")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_users_empty_pool(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/users", headers=_auth_headers(token))
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_create_user_generates_uuid_and_dispatches_add_user(
    client: AsyncClient, seeded_admin: dict
) -> None:
    entry_agent = FakeAgent()
    try:
        entry_node_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_node_id)
        entry_agent.received_commands.clear()  # drop the cascade-creation commands

        response = await client.post(
            "/users",
            json={"name": "alice", "cascade_ids": [cascade_id]},
            headers=_auth_headers(token),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "alice"
        assert body["uuid"]
        assert body["cascade_ids"] == [cascade_id]
        assert body["revoked_at"] is None
        # id and uuid are both server-generated and must differ from anything the
        # client could have supplied (the request body has no id/uuid field at all).
        assert body["id"]

        assert len(entry_agent.received_commands) == 1
        command = entry_agent.received_commands[0]
        assert command.WhichOneof("op") == "add_user"
        assert command.add_user.uuid == body["uuid"]
        assert command.add_user.email == body["uuid"]

        list_response = await client.get("/users", headers=_auth_headers(token))
        assert list_response.json()["total"] == 1

        get_response = await client.get(f"/users/{body['id']}", headers=_auth_headers(token))
        assert get_response.status_code == 200
        assert get_response.json()["uuid"] == body["uuid"]
    finally:
        await entry_agent.close()


@pytest.mark.asyncio
async def test_create_user_unknown_cascade_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.post(
        "/users",
        json={"name": "bob", "cascade_ids": ["does-not-exist"]},
        headers=_auth_headers(token),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_revoke_user_dispatches_remove_user_and_marks_revoked(
    client: AsyncClient, seeded_admin: dict
) -> None:
    entry_agent = FakeAgent()
    try:
        entry_node_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_node_id)

        create_response = await client.post(
            "/users",
            json={"name": "carol", "cascade_ids": [cascade_id]},
            headers=_auth_headers(token),
        )
        user_id = create_response.json()["uuid"], create_response.json()["id"]
        user_uuid, user_id = user_id
        entry_agent.received_commands.clear()

        delete_response = await client.delete(f"/users/{user_id}", headers=_auth_headers(token))
        assert delete_response.status_code == 204

        assert len(entry_agent.received_commands) == 1
        command = entry_agent.received_commands[0]
        assert command.WhichOneof("op") == "remove_user"
        assert command.remove_user.email == user_uuid

        get_response = await client.get(f"/users/{user_id}", headers=_auth_headers(token))
        assert get_response.status_code == 200
        body = get_response.json()
        assert body["revoked_at"] is not None
        assert body["cascade_ids"] == []
    finally:
        await entry_agent.close()


@pytest.mark.asyncio
async def test_get_missing_user_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/users/does-not-exist", headers=_auth_headers(token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_revoke_missing_user_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.delete("/users/does-not-exist", headers=_auth_headers(token))
    assert response.status_code == 404
