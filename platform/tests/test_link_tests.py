"""httpx tests for the three REST paths TZ-5 §4.3 describes: POST /cascades/{id}/test,
GET /tests/{test_id}, GET /cascades/{id}/tests - all real now (services/link_tests.py),
previously 501. See tests/test_grpc_dispatch.py for the lower-level gRPC-side
RunTest/TestResult correlation test (drives the servicer directly, no HTTP)."""
from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import agent_control_pb2 as pb
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


async def _create_offline_node() -> str:
    async with session_scope() as db:
        node = Node(label="offline-entry", role=NodeRole.ENTRY, status=NodeStatus.HEALTHY)
        db.add(node)
        await db.flush()
        return node.id


async def _wait_for_resolution(client: AsyncClient, token: str, test_id: str, *, timeout: float = 5.0) -> dict:
    """LinkTest resolution happens in a background task (services/link_tests.py) after
    POST /cascades/{id}/test already returned 202 - poll GET /tests/{id} until
    `outcome` is no longer null instead of assuming any particular timing."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        response = await client.get(f"/tests/{test_id}", headers=_auth_headers(token))
        assert response.status_code == 200
        body = response.json()
        if body["outcome"] is not None:
            return body
        if loop.time() > deadline:
            raise AssertionError(f"LinkTest {test_id} did not resolve within {timeout}s: {body}")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_start_cascade_test_returns_202_with_test_id(client: AsyncClient, seeded_admin: dict) -> None:
    agent = FakeAgent()
    try:
        entry_node_id = await agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_node_id)

        response = await client.post(f"/cascades/{cascade_id}/test", headers=_auth_headers(token))
        assert response.status_code == 202
        body = response.json()
        assert body["test_id"]

        # The dispatched op reached the fake Agent as a RunTest, distinct from the
        # AddInbound/AddOutbound/AddRoutingRule cascade-assembly commands from
        # _create_cascade() above.
        run_test_commands = [c for c in agent.received_commands if c.WhichOneof("op") == "run_test"]
        assert len(run_test_commands) == 1
        assert run_test_commands[0].run_test.test_id == body["test_id"]
        assert run_test_commands[0].command_id == body["test_id"]  # shared correlation id, see servicer.py
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_start_cascade_test_unknown_cascade_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.post("/cascades/does-not-exist/test", headers=_auth_headers(token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_link_test_resolves_to_real_agent_outcome(client: AsyncClient, seeded_admin: dict) -> None:
    agent = FakeAgent(
        test_result_outcome=pb.TEST_OUTCOME_SUCCESS, test_result_latency_ms=123, test_result_detail="probe ok"
    )
    try:
        entry_node_id = await agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_node_id)

        start_response = await client.post(f"/cascades/{cascade_id}/test", headers=_auth_headers(token))
        test_id = start_response.json()["test_id"]

        resolved = await _wait_for_resolution(client, token, test_id)
        assert resolved["outcome"] == "success"
        assert resolved["latency_ms"] == 123
        assert resolved["detail"] == "probe ok"
        assert resolved["cascade_id"] == cascade_id
        assert resolved["initiated_by"] == seeded_admin["username"]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_link_test_surfaces_specific_failure_step(client: AsyncClient, seeded_admin: dict) -> None:
    """TZ-5 §4.3's core requirement: a failed test-request reports *which step* failed,
    not a bare ok/fail - TLS_HANDSHAKE_FAILED here, distinct from e.g. TIMEOUT."""
    agent = FakeAgent(
        test_result_outcome=pb.TEST_OUTCOME_TLS_HANDSHAKE_FAILED,
        test_result_latency_ms=800,
        test_result_detail="TLS handshake with exit node timed out",
    )
    try:
        entry_node_id = await agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_node_id)

        start_response = await client.post(f"/cascades/{cascade_id}/test", headers=_auth_headers(token))
        test_id = start_response.json()["test_id"]

        resolved = await _wait_for_resolution(client, token, test_id)
        assert resolved["outcome"] == "tls_handshake_failed"
        assert resolved["detail"] == "TLS handshake with exit node timed out"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_link_test_against_offline_entry_node_records_timeout(
    client: AsyncClient, seeded_admin: dict
) -> None:
    offline_entry_node_id = await _create_offline_node()
    agent_for_cascade_assembly = FakeAgent()
    try:
        # A Cascade needs *a* valid entry node to be created at all; simulate the
        # entry node going offline again right after assembly (same "temporarily
        # disconnected" scenario as test_cascades.py's DEGRADED-status test).
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, offline_entry_node_id)

        start_response = await client.post(f"/cascades/{cascade_id}/test", headers=_auth_headers(token))
        assert start_response.status_code == 202
        test_id = start_response.json()["test_id"]

        # No live Agent session for this node - services/link_tests.py resolves this
        # synchronously (NodeNotConnectedError, no Agent to wait on), so the row should
        # already be resolved by the time the 202 response came back.
        get_response = await client.get(f"/tests/{test_id}", headers=_auth_headers(token))
        assert get_response.status_code == 200
        body = get_response.json()
        assert body["outcome"] == "timeout"
        assert "no live" in body["detail"] or "session" in body["detail"]
    finally:
        await agent_for_cascade_assembly.close()


@pytest.mark.asyncio
async def test_get_test_unknown_id_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/tests/does-not-exist", headers=_auth_headers(token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_cascade_tests_paginated_history(client: AsyncClient, seeded_admin: dict) -> None:
    agent = FakeAgent()
    try:
        entry_node_id = await agent.connect(NodeRole.ENTRY)
        token = await _login(client, seeded_admin)
        cascade_id = await _create_cascade(client, token, entry_node_id)

        test_ids = []
        for _ in range(3):
            start_response = await client.post(f"/cascades/{cascade_id}/test", headers=_auth_headers(token))
            test_ids.append(start_response.json()["test_id"])
        for test_id in test_ids:
            await _wait_for_resolution(client, token, test_id)

        list_response = await client.get(f"/cascades/{cascade_id}/tests", headers=_auth_headers(token))
        assert list_response.status_code == 200
        body = list_response.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        returned_ids = {item["id"] for item in body["items"]}
        assert returned_ids == set(test_ids)

        page_response = await client.get(
            f"/cascades/{cascade_id}/tests", params={"page": 1, "page_size": 2}, headers=_auth_headers(token)
        )
        assert page_response.json()["total"] == 3
        assert len(page_response.json()["items"]) == 2
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_list_cascade_tests_unknown_cascade_is_404(client: AsyncClient, seeded_admin: dict) -> None:
    token = await _login(client, seeded_admin)
    response = await client.get("/cascades/does-not-exist/tests", headers=_auth_headers(token))
    assert response.status_code == 404
