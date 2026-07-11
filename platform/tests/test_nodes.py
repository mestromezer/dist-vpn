"""GET/POST /nodes and GET/PATCH/DELETE /nodes/{node_id} (contracts/rest/openapi.yaml)."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _login(client: AsyncClient, seeded_admin: dict) -> str:
    response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_nodes_requires_authentication(client: AsyncClient, db_engine: None) -> None:
    response = await client.get("/nodes")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_nodes_empty_pool(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/nodes", headers=_auth_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_create_enrollment_token(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.post(
        "/nodes",
        json={"requested_role": "entry", "label": "ru-entry-01"},
        headers=_auth_headers(token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["enrollment_token"]
    assert len(body["enrollment_token"]) >= 32
    assert body["expires_at"]


@pytest.mark.asyncio
async def test_node_lifecycle_get_patch_delete(client: AsyncClient, seeded_admin: dict) -> None:
    from dist_vpn_platform.db import session_scope
    from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus

    # Node rows are normally created by the gRPC enrollment handshake
    # (grpc_server/servicer.py); inserted directly here to exercise the REST CRUD
    # surface in isolation, per the task scope (node list/create-enrollment endpoints).
    async with session_scope() as db:
        node = Node(label="test-node", role=NodeRole.EXIT, status=NodeStatus.HEALTHY)
        db.add(node)
        await db.flush()
        node_id = node.id

    token = await _login(client, seeded_admin)
    headers = _auth_headers(token)

    get_response = await client.get(f"/nodes/{node_id}", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["label"] == "test-node"

    patch_response = await client.patch(
        f"/nodes/{node_id}", json={"label": "renamed-node"}, headers=headers
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["label"] == "renamed-node"

    list_response = await client.get("/nodes", headers=headers)
    assert list_response.json()["total"] == 1

    delete_response = await client.delete(f"/nodes/{node_id}", headers=headers)
    assert delete_response.status_code == 202

    get_after_delete = await client.get(f"/nodes/{node_id}", headers=headers)
    assert get_after_delete.json()["status"] == "decommissioned"


@pytest.mark.asyncio
async def test_get_missing_node_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/nodes/does-not-exist", headers=_auth_headers(token))
    assert response.status_code == 404
