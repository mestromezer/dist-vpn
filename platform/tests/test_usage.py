"""GET /usage (contracts/rest/openapi.yaml) - filtered read over the UsageStat rows
grpc_server/servicer.py::_handle_telemetry already accumulates into hourly buckets."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.models.usage_stat import UsageStat
from dist_vpn_platform.models.user import User


async def _login(client: AsyncClient, seeded_admin: dict) -> str:
    response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_usage(*, node_id: str, user_id: str | None, scope: str, raw_key: str, period_start: datetime) -> None:
    async with session_scope() as db:
        db.add(
            UsageStat(
                scope=scope,
                raw_key=raw_key,
                user_id=user_id,
                node_id=node_id,
                period_start=period_start,
                uplink_bytes=1000,
                downlink_bytes=2000,
            )
        )


@pytest.mark.asyncio
async def test_usage_requires_authentication(client: AsyncClient, db_engine: None) -> None:
    response = await client.get("/usage")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_usage_empty_by_default(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/usage", headers=_auth_headers(token))
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_usage_filters_by_user_id_and_node_id(client: AsyncClient, seeded_admin: dict) -> None:
    async with session_scope() as db:
        node_a = Node(label="node-a", role=NodeRole.ENTRY, status=NodeStatus.HEALTHY)
        node_b = Node(label="node-b", role=NodeRole.ENTRY, status=NodeStatus.HEALTHY)
        user_a = User(name="alice")
        user_b = User(name="bob")
        db.add_all([node_a, node_b, user_a, user_b])
        await db.flush()
        node_a_id, node_b_id = node_a.id, node_b.id
        user_a_id, user_b_id = user_a.id, user_b.id

    now = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    await _seed_usage(node_id=node_a_id, user_id=user_a_id, scope="user", raw_key=user_a_id, period_start=now)
    await _seed_usage(node_id=node_b_id, user_id=user_b_id, scope="user", raw_key=user_b_id, period_start=now)
    await _seed_usage(node_id=node_a_id, user_id=None, scope="inbound", raw_key="hopa-in-x", period_start=now)

    token = await _login(client, seeded_admin)

    by_user = await client.get("/usage", params={"user_id": user_a_id}, headers=_auth_headers(token))
    assert by_user.status_code == 200
    assert len(by_user.json()) == 1
    assert by_user.json()[0]["user_id"] == user_a_id

    by_node = await client.get("/usage", params={"node_id": node_a_id}, headers=_auth_headers(token))
    assert by_node.status_code == 200
    # Both the user-scoped and inbound-scoped rows on node_a - no scope filter exists.
    assert len(by_node.json()) == 2
    assert all(row["node_id"] == node_a_id for row in by_node.json())

    # An inbound-scoped row has no user - UsageStatOut.user_id must reflect that
    # rather than silently coercing/omitting it.
    inbound_row = next(row for row in by_node.json() if row["user_id"] is None)
    assert inbound_row["uplink_bytes"] == 1000
    assert inbound_row["downlink_bytes"] == 2000


@pytest.mark.asyncio
async def test_usage_filters_by_time_range(client: AsyncClient, seeded_admin: dict) -> None:
    async with session_scope() as db:
        node = Node(label="node-c", role=NodeRole.ENTRY, status=NodeStatus.HEALTHY)
        db.add(node)
        await db.flush()
        node_id = node.id

    base = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    await _seed_usage(node_id=node_id, user_id=None, scope="inbound", raw_key="a", period_start=base)
    await _seed_usage(
        node_id=node_id, user_id=None, scope="inbound", raw_key="a", period_start=base + timedelta(hours=5)
    )
    await _seed_usage(
        node_id=node_id, user_id=None, scope="inbound", raw_key="a", period_start=base + timedelta(hours=10)
    )

    token = await _login(client, seeded_admin)
    response = await client.get(
        "/usage",
        params={
            "node_id": node_id,
            "from": (base + timedelta(hours=3)).isoformat(),
            "to": (base + timedelta(hours=8)).isoformat(),
        },
        headers=_auth_headers(token),
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
