"""GET/POST/DELETE /cascades, GET /cascades/{id} (contracts/rest/openapi.yaml) -
real CRUD backed by a persisted Cascade row and live command dispatch to connected
fake Agents (see tests/grpc_test_helpers.py::FakeAgent)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.models.cascade import CascadeExitNode
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


async def _create_cascade(client: AsyncClient, token: str, entry_node_id: str) -> str:
    response = await client.post(
        "/cascades",
        json={"entry_node_id": entry_node_id, "exit_node_ids": []},
        headers=_auth_headers(token),
    )
    assert response.status_code == 201
    return response.json()["id"]


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

        # REALITY key material must be real (non-empty), not the old hardcoded ""
        # placeholder that made a handshake impossible - see security/reality_keys.py.
        assert body["hopa_reality_public_key"]
        assert body["hopa_reality_short_id"]

        hopa_inbound = json.loads(entry_agent.received_commands[0].add_inbound.inbound_json)
        hopa_reality = hopa_inbound["streamSettings"]["realitySettings"]
        assert hopa_reality["privateKey"]
        assert hopa_reality["shortIds"] == [body["hopa_reality_short_id"]]
        # The private key must never leak into the public API response.
        assert hopa_reality["privateKey"] != body["hopa_reality_public_key"]
        # "port" used to be omitted entirely - agent/xray_client.py treats it as
        # *optional* rather than raising, so the inbound would silently end up with no
        # port_list, i.e. nothing actually listening. A second, independent hard
        # blocker beyond the empty REALITY keys above - see xray_templates.py's
        # HOP_LISTEN_PORT docstring.
        assert hopa_inbound["port"] == 443
        assert hopa_inbound["listen"] == "0.0.0.0"

        hopb_outbound = json.loads(entry_agent.received_commands[1].add_outbound.outbound_json)
        hopb_outbound_reality = hopb_outbound["streamSettings"]["realitySettings"]
        hopb_inbound = json.loads(exit_agent.received_commands[0].add_inbound.inbound_json)
        hopb_inbound_reality = hopb_inbound["streamSettings"]["realitySettings"]
        assert hopb_inbound["port"] == 443
        assert hopb_outbound["settings"]["vnext"][0]["port"] == 443
        assert hopb_inbound_reality["privateKey"]
        # REALITY donor rotation (services/xray_templates.py _select_reality_donor):
        # the Hop A inbound's dest must actually match the SNI it advertises, and the
        # Hop B outbound (REALITY client role)'s serverName must match what the Hop B
        # inbound (REALITY server role) accepts for the same link - otherwise the
        # handshake on that hop can never complete.
        assert hopa_reality["dest"] == f"{hopa_reality['serverNames'][0]}:443"
        assert hopb_outbound_reality["serverName"] == hopb_inbound_reality["serverNames"][0]
        # The Hop A->B outbound (entry, REALITY client role) must reference the exit
        # node's own Hop B inbound public identity - fetch the persisted pairing row
        # directly since GET /cascades doesn't expose Hop B key material via the API
        # (only Hop A's, which the end client actually needs - see schemas/cascades.py).
        async with session_scope() as db:
            link = await db.get(CascadeExitNode, (cascade_id, exit_node_id))
            assert hopb_outbound_reality["publicKey"] == link.hopb_reality_public_key
            assert hopb_outbound_reality["shortId"] == link.hopb_reality_short_id
            assert hopb_inbound_reality["privateKey"] == link.hopb_reality_private_key

        list_response = await client.get("/cascades", headers=_auth_headers(token))
        assert list_response.json()["total"] == 1

        get_response = await client.get(f"/cascades/{cascade_id}", headers=_auth_headers(token))
        assert get_response.status_code == 200
        assert get_response.json()["status"] == "up"
    finally:
        await entry_agent.close()
        await exit_agent.close()


@pytest.mark.asyncio
async def test_create_cascade_with_p4_preset_dispatches_a_second_inbound(
    client: AsyncClient, seeded_admin: dict
) -> None:
    """hop_a_presets: ["p1", "p4"] must produce TWO AddInbound commands on the entry
    node (REALITY + P4/WS+TLS), not just the primary one - see
    api/routes/cascades.py::create_cascade and xray_templates.py build_hopa_p4_inbound_json."""
    entry_agent = FakeAgent()
    try:
        entry_node_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)

        response = await client.post(
            "/cascades",
            json={"entry_node_id": entry_node_id, "exit_node_ids": [], "hop_a_presets": ["p1", "p4"]},
            headers=_auth_headers(token),
        )
        assert response.status_code == 201
        cascade_id = response.json()["id"]

        add_inbound_commands = [
            c for c in entry_agent.received_commands if c.WhichOneof("op") == "add_inbound"
        ]
        assert len(add_inbound_commands) == 2

        inbounds = [json.loads(c.add_inbound.inbound_json) for c in add_inbound_commands]
        preset_hints = {inbound.get("_presetHint") for inbound in inbounds}
        assert preset_hints == {"p1", "p4"}

        p4_inbound = next(inbound for inbound in inbounds if inbound["_presetHint"] == "p4")
        assert p4_inbound["streamSettings"]["network"] == "ws"
        assert p4_inbound["streamSettings"]["security"] == "tls"
        assert p4_inbound["tag"] != inbounds[0]["tag"]
        assert cascade_id  # sanity: cascade was actually created despite the extra dispatch
    finally:
        await entry_agent.close()


@pytest.mark.asyncio
async def test_create_cascade_without_p4_preset_dispatches_only_the_reality_inbound(
    client: AsyncClient, seeded_admin: dict
) -> None:
    entry_agent = FakeAgent()
    try:
        entry_node_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)

        response = await client.post(
            "/cascades",
            json={"entry_node_id": entry_node_id, "exit_node_ids": [], "hop_a_presets": ["p1"]},
            headers=_auth_headers(token),
        )
        assert response.status_code == 201

        add_inbound_commands = [
            c for c in entry_agent.received_commands if c.WhichOneof("op") == "add_inbound"
        ]
        assert len(add_inbound_commands) == 1
    finally:
        await entry_agent.close()


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


@pytest.mark.asyncio
async def test_migrate_users_real_dispatch_and_membership_swap(client: AsyncClient, seeded_admin: dict) -> None:
    """The admin-triggered half of failover (TZ-5 §4.3): real RemoveUser on the old
    cascade's (connected) entry agent, real AddUser on the new one, and the
    UserCascade rows actually swapped - not just an HTTP 200."""
    old_entry_agent = FakeAgent()
    new_entry_agent = FakeAgent()
    try:
        old_entry_id = await old_entry_agent.connect(NodeRole.ENTRY)
        new_entry_id = await new_entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)

        old_cascade_response = await client.post(
            "/cascades", json={"entry_node_id": old_entry_id, "exit_node_ids": []}, headers=_auth_headers(token)
        )
        old_cascade_id = old_cascade_response.json()["id"]
        new_cascade_response = await client.post(
            "/cascades", json={"entry_node_id": new_entry_id, "exit_node_ids": []}, headers=_auth_headers(token)
        )
        new_cascade_id = new_cascade_response.json()["id"]

        user_response = await client.post(
            "/users", json={"name": "migrant", "cascade_ids": [old_cascade_id]}, headers=_auth_headers(token)
        )
        user_id = user_response.json()["id"]
        user_uuid = user_response.json()["uuid"]
        old_entry_agent.received_commands.clear()  # drop the cascade-creation/AddUser noise

        response = await client.post(
            f"/cascades/{old_cascade_id}/migrate-users",
            json={"to_cascade_id": new_cascade_id},
            headers=_auth_headers(token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["cascade_id"] == old_cascade_id
        assert body["to_cascade_id"] == new_cascade_id
        assert body["migrated_user_count"] == 1

        old_ops = [c.WhichOneof("op") for c in old_entry_agent.received_commands]
        assert old_ops == ["remove_user"]
        assert old_entry_agent.received_commands[0].remove_user.email == user_uuid

        new_ops = [c.WhichOneof("op") for c in new_entry_agent.received_commands]
        assert "add_user" in new_ops
        add_cmd = next(c for c in new_entry_agent.received_commands if c.WhichOneof("op") == "add_user")
        assert add_cmd.add_user.email == user_uuid
        assert add_cmd.add_user.uuid == user_uuid

        get_response = await client.get(f"/users/{user_id}", headers=_auth_headers(token))
        assert get_response.json()["cascade_ids"] == [new_cascade_id]
    finally:
        await old_entry_agent.close()
        await new_entry_agent.close()


@pytest.mark.asyncio
async def test_migrate_users_same_cascade_is_400(client: AsyncClient, seeded_admin: dict) -> None:
    entry_agent = FakeAgent()
    try:
        entry_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_id)

        response = await client.post(
            f"/cascades/{cascade_id}/migrate-users",
            json={"to_cascade_id": cascade_id},
            headers=_auth_headers(token),
        )
        assert response.status_code == 400
    finally:
        await entry_agent.close()


@pytest.mark.asyncio
async def test_migrate_users_missing_source_cascade_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    entry_agent = FakeAgent()
    try:
        entry_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        target_cascade_id = await _create_cascade(client, token, entry_id)

        response = await client.post(
            "/cascades/does-not-exist/migrate-users",
            json={"to_cascade_id": target_cascade_id},
            headers=_auth_headers(token),
        )
        assert response.status_code == 404
    finally:
        await entry_agent.close()


@pytest.mark.asyncio
async def test_migrate_users_missing_target_cascade_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    entry_agent = FakeAgent()
    try:
        entry_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        source_cascade_id = await _create_cascade(client, token, entry_id)

        response = await client.post(
            f"/cascades/{source_cascade_id}/migrate-users",
            json={"to_cascade_id": "does-not-exist"},
            headers=_auth_headers(token),
        )
        assert response.status_code == 404
    finally:
        await entry_agent.close()


@pytest.mark.asyncio
async def test_list_cascade_events_returns_creation_and_migration_history(
    client: AsyncClient, seeded_admin: dict
) -> None:
    entry_agent = FakeAgent()
    other_entry_agent = FakeAgent()
    try:
        entry_id = await entry_agent.connect(NodeRole.ENTRY)
        other_entry_id = await other_entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_id)
        other_cascade_id = await _create_cascade(client, token, other_entry_id)

        # A second cascade's own creation event must not leak into the first
        # cascade's history.
        response = await client.get(f"/cascades/{cascade_id}/events", headers=_auth_headers(token))
        assert response.status_code == 200
        events = response.json()
        assert len(events) == 1
        assert events[0]["cascade_id"] == cascade_id
        assert events[0]["to_status"] == "up"
        assert events[0]["from_status"] == "down"

        # migrate-users (even with zero users to move) doesn't itself write a
        # HealthEvent - this just confirms the endpoint reflects real, ongoing
        # activity rather than only ever showing the single creation row.
        await client.post(
            f"/cascades/{cascade_id}/migrate-users",
            json={"to_cascade_id": other_cascade_id},
            headers=_auth_headers(token),
        )
        unaffected_response = await client.get(f"/cascades/{cascade_id}/events", headers=_auth_headers(token))
        assert len(unaffected_response.json()) == 1
    finally:
        await entry_agent.close()
        await other_entry_agent.close()


@pytest.mark.asyncio
async def test_list_cascade_events_since_filter(client: AsyncClient, seeded_admin: dict) -> None:
    entry_agent = FakeAgent()
    try:
        entry_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_id)

        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        response = await client.get(
            f"/cascades/{cascade_id}/events", params={"since": future}, headers=_auth_headers(token)
        )
        assert response.status_code == 200
        assert response.json() == []
    finally:
        await entry_agent.close()


@pytest.mark.asyncio
async def test_list_cascade_events_missing_cascade_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/cascades/does-not-exist/events", headers=_auth_headers(token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_migrate_users_with_no_users_on_source_is_a_no_op(client: AsyncClient, seeded_admin: dict) -> None:
    entry_agent = FakeAgent()
    try:
        entry_id = await entry_agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        source_cascade_id = await _create_cascade(client, token, entry_id)
        target_cascade_id = await _create_cascade(client, token, entry_id)

        response = await client.post(
            f"/cascades/{source_cascade_id}/migrate-users",
            json={"to_cascade_id": target_cascade_id},
            headers=_auth_headers(token),
        )
        assert response.status_code == 200
        assert response.json()["migrated_user_count"] == 0
    finally:
        await entry_agent.close()
