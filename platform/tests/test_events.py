"""WS /events (TZ-5 §4.3, §7) - connect, trigger an event server-side, confirm the
message arrives as JSON. See services/event_bus.py for the in-process pub/sub broker
this route subscribes to, and its two publish call sites this file exercises:
grpc_server/servicer.py's heartbeat-driven status transitions, and
services/link_tests.py's LinkTest resolution (the follow-up to
POST /cascades/{id}/test).

A plain `httpx.ASGITransport` (used by the `client` fixture in conftest.py for the
REST tests) cannot perform a WebSocket upgrade at all - it has no `network_stream`
extension for httpx_ws to hijack. `httpx_ws.transport.ASGIWebSocketTransport` is the
websocket-capable counterpart (still an in-process ASGI transport - no real socket,
same event loop as the rest of this async test, so there is no cross-event-loop hazard
with `event_bus`'s plain `asyncio.Queue` the way there would be with
`starlette.testclient.TestClient`, which runs the ASGI app in a separate thread/loop).

`_ws_client()` below is a plain `@asynccontextmanager` helper used *inside* each test
body (`async with _ws_client(app) as ws_client:`), not a pytest fixture - deliberately.
`ASGIWebSocketTransport.__aenter__`/`__aexit__` open/close an anyio cancel scope tied
to whichever asyncio Task calls them. pytest-asyncio (>=0.23) runs an async-generator
*fixture*'s post-yield teardown in a brand new Task (`_create_task_in_context`),
distinct from the Task that ran the fixture's setup - so entering the transport during
fixture setup and exiting it during fixture teardown trips anyio's "attempted to exit
cancel scope in a different task than it was entered in" guard. A test *function*
body, by contrast, always runs start-to-finish as a single Task under pytest-asyncio,
so opening and closing the same transport entirely within one test's body keeps
enter/exit on the same Task and avoids the mismatch.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx_ws import WebSocketDisconnect, WebSocketUpgradeError, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport

from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.grpc_server.servicer import AgentControlServicer
from dist_vpn_platform.models.node import NodeRole
from tests.grpc_test_helpers import FakeAgent


@asynccontextmanager
async def _ws_client(app):
    transport = ASGIWebSocketTransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as ac:
        yield ac


async def _login(client: httpx.AsyncClient, seeded_admin: dict) -> str:
    response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_ws_events_rejects_missing_token(app) -> None:
    async with _ws_client(app) as ws_client:
        with pytest.raises((WebSocketUpgradeError, WebSocketDisconnect)):
            async with aconnect_ws("/events", ws_client):
                pass


@pytest.mark.asyncio
async def test_ws_events_rejects_invalid_token(app) -> None:
    async with _ws_client(app) as ws_client:
        with pytest.raises((WebSocketUpgradeError, WebSocketDisconnect)):
            async with aconnect_ws("/events?token=not-a-real-token", ws_client):
                pass


@pytest.mark.asyncio
async def test_ws_events_receives_health_event_on_heartbeat_transition(app, seeded_admin: dict) -> None:
    """Wires into heartbeat status transitions (grpc_server/servicer.py
    _handle_heartbeat -> services/event_bus.py::publish_health_event) - the
    ENROLLING -> HEALTHY transition a node's first successful heartbeat causes."""
    async with _ws_client(app) as ws_client:
        token = await _login(ws_client, seeded_admin)
        agent = FakeAgent()
        try:
            node_id = await agent.connect(NodeRole.ENTRY)  # Node row created, status=ENROLLING

            async with aconnect_ws(f"/events?token={token}", ws_client) as ws:
                await AgentControlServicer()._handle_heartbeat(
                    pb.Heartbeat(
                        node_id=node_id,
                        health=pb.NodeHealth(
                            xray_container_running=True, cpu_percent=1.0, mem_percent=1.0, disk_percent=1.0
                        ),
                        timestamp_unix=int(time.time()),
                    )
                )
                message = await asyncio.wait_for(ws.receive_json(), timeout=5.0)

            assert message["type"] == "health_event"
            assert message["data"]["node_id"] == node_id
            assert message["data"]["from_status"] == "enrolling"
            assert message["data"]["to_status"] == "healthy"
        finally:
            await agent.close()


@pytest.mark.asyncio
async def test_ws_events_receives_resolved_link_test(app, seeded_admin: dict) -> None:
    """Wires into Part B's resolved tests (services/link_tests.py::_resolve ->
    services/event_bus.py::publish_link_test_resolved), following an actual
    POST /cascades/{id}/test -> FakeAgent TestResult round trip."""
    async with _ws_client(app) as ws_client:
        token = await _login(ws_client, seeded_admin)
        agent = FakeAgent(
            test_result_outcome=pb.TEST_OUTCOME_SUCCESS, test_result_latency_ms=55, test_result_detail="ok"
        )
        try:
            entry_node_id = await agent.connect(NodeRole.ENTRY)
            create_response = await ws_client.post(
                "/cascades",
                json={"entry_node_id": entry_node_id, "exit_node_ids": []},
                headers=_auth_headers(token),
            )
            assert create_response.status_code == 201
            cascade_id = create_response.json()["id"]

            async with aconnect_ws(f"/events?token={token}", ws_client) as ws:
                start_response = await ws_client.post(
                    f"/cascades/{cascade_id}/test", headers=_auth_headers(token)
                )
                assert start_response.status_code == 202
                test_id = start_response.json()["test_id"]

                message = await asyncio.wait_for(ws.receive_json(), timeout=5.0)

            assert message["type"] == "link_test"
            assert message["data"]["id"] == test_id
            assert message["data"]["cascade_id"] == cascade_id
            assert message["data"]["outcome"] == "success"
            assert message["data"]["latency_ms"] == 55
        finally:
            await agent.close()


@pytest.mark.asyncio
async def test_ws_events_multiple_subscribers_all_receive_the_same_event(app, seeded_admin: dict) -> None:
    """services/event_bus.py fans a single publish() out to every subscriber - not
    just the first one connected."""
    async with _ws_client(app) as ws_client:
        token = await _login(ws_client, seeded_admin)
        agent = FakeAgent()
        try:
            node_id = await agent.connect(NodeRole.ENTRY)

            async with aconnect_ws(f"/events?token={token}", ws_client) as ws_a:
                async with aconnect_ws(f"/events?token={token}", ws_client) as ws_b:
                    await AgentControlServicer()._handle_heartbeat(
                        pb.Heartbeat(
                            node_id=node_id,
                            health=pb.NodeHealth(
                                xray_container_running=True, cpu_percent=1.0, mem_percent=1.0, disk_percent=1.0
                            ),
                            timestamp_unix=int(time.time()),
                        )
                    )
                    message_a = await asyncio.wait_for(ws_a.receive_json(), timeout=5.0)
                    message_b = await asyncio.wait_for(ws_b.receive_json(), timeout=5.0)

            assert message_a == message_b
            assert message_a["type"] == "health_event"
        finally:
            await agent.close()
