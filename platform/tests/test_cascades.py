"""GET/POST/DELETE /cascades, GET /cascades/{id} (contracts/rest/openapi.yaml) -
real CRUD backed by a persisted Cascade row and live command dispatch to connected
fake Agents (see tests/grpc_test_helpers.py::FakeAgent)."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from tests.grpc_test_helpers import FakeAgent


async def _login(client: AsyncClient, seeded_admin: dict) -> str:
    response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_offline_node(role: NodeRole) -> str:
    """A Node row that exists in the DB but has no live AgentControl session -
    simulates a temporarily disconnected Agent for the DEGRADED-status test below."""
    async with session_scope() as db:
        node = Node(label="offline-node", role=role, status=NodeStatus.HEALTHY)
        db.add(node)
        await db.flush()
        return node.id


@pytest.mark.asyncio
async def test_list_cascades_requires_authentication(client: AsyncClient, db_engine: None) -> None:
    response = await client.get("/cascades")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_cascades_empty_pool(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/cascades", headers=_auth_headers(token))
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_create_cascade_dispatches_commands_and_reports_up(
    client: AsyncClient, seeded_admin: dict
) -> None:
    entry_agent = FakeAgent()
    exit_agent = FakeAgent()
    try:
        entry_node_id = await entry_agent.connect(NodeRole.ENTRY)
        exit_node_id = await exit_agent.connect(NodeRole.EXIT)

        token = await _login(client, seeded_admin)
        response = await client.post(
            "/cascades",
            json={
                "entry_node_id": entry_node_id,
                "exit_node_ids": [exit_node_id],
                "hop_a_presets": ["p1"],
            },
            headers=_auth_headers(token),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["entry_node_id"] == entry_node_id
        assert body["exit_node_ids"] == [exit_node_id]
        assert body["status"] == "up"
        cascade_id = body["id"]

        # Entry node should have received: AddInbound (Hop A) + AddOutbound (Hop B
        # candidate) + AddRoutingRule (balancer wiring) - see api/routes/cascades.py.
        entry_ops = [c.WhichOneof("op") for c in entry_agent.received_commands]
        assert entry_ops == ["add_inbound", "add_outbound", "add_routing_rule"]

        # Exit node should have received just the Hop B inbound.
        exit_ops = [c.WhichOneof("op") for c in exit_agent.received_commands]
        assert exit_ops == ["add_inbound"]

        list_response = await client.get("/cascades", headers=_auth_headers(token))
        assert list_response.json()["total"] == 1

        get_response = await client.get(f"/cascades/{cascade_id}", headers=_auth_headers(token))
        assert get_response.status_code == 200
        assert get_response.json()["status"] == "up"
    finally:
        await entry_agent.close()
        await exit_agent.close()


@pytest.mark.asyncio
async def test_create_cascade_with_offline_exit_node_is_degraded(
    client: AsyncClient, seeded_admin: dict
) -> None:
    entry_agent = FakeAgent()
    try:
        entry_node_id = await entry_agent.connect(NodeRole.ENTRY)
        offline_exit_node_id = await _create_offline_node(NodeRole.EXIT)

        token = await _login(client, seeded_admin)
        response = await client.post(
            "/cascades",
            json={"entry_node_id": entry_node_id, "exit_node_ids": [offline_exit_node_id]},
            headers=_auth_headers(token),
        )
        assert response.status_code == 201
        # Entry-side commands still went through (entry agent is connected); the exit
        # node's AddInbound could not be delivered (no live session) - degraded, not a
        # failed request (see api/command_dispatch.py's best-effort rationale).
        assert response.json()["status"] == "degraded"
    finally:
        await entry_agent.close()


@pytest.mark.asyncio
async def test_create_cascade_unknown_entry_node_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.post(
        "/cascades",
        json={"entry_node_id": "does-not-exist", "exit_node_ids": []},
        headers=_auth_headers(token),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_cascade_dispatches_remove_commands(client: AsyncClient, seeded_admin: dict) -> None:
    entry_agent = FakeAgent()
    exit_agent = FakeAgent()
    try:
        entry_node_id = await entry_agent.connect(NodeRole.ENTRY)
        exit_node_id = await exit_agent.connect(NodeRole.EXIT)

        token = await _login(client, seeded_admin)
        create_response = await client.post(
            "/cascades",
            json={"entry_node_id": entry_node_id, "exit_node_ids": [exit_node_id]},
            headers=_auth_headers(token),
        )
        cascade_id = create_response.json()["id"]
        entry_agent.received_commands.clear()
        exit_agent.received_commands.clear()

        delete_response = await client.delete(f"/cascades/{cascade_id}", headers=_auth_headers(token))
        assert delete_response.status_code == 204

        entry_ops = [c.WhichOneof("op") for c in entry_agent.received_commands]
        assert entry_ops == ["remove_inbound", "remove_routing_rule", "remove_outbound"]
        exit_ops = [c.WhichOneof("op") for c in exit_agent.received_commands]
        assert exit_ops == ["remove_inbound"]

        get_response = await client.get(f"/cascades/{cascade_id}", headers=_auth_headers(token))
        assert get_response.status_code == 404

        list_response = await client.get("/cascades", headers=_auth_headers(token))
        assert list_response.json()["total"] == 0
    finally:
        await entry_agent.close()
        await exit_agent.close()


@pytest.mark.asyncio
async def test_get_missing_cascade_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/cascades/does-not-exist", headers=_auth_headers(token))
    assert response.status_code == 404
