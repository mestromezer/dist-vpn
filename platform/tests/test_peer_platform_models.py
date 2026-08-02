"""PeerPlatform / RemoteExitCandidate / ExitLinkAllocation - the new models that let a
Cascade's exit-candidate pool span two separate Platform databases once Platform is
split into RU-segment/foreign-segment instances (see models/peer_platform.py's module
docstring for the full rationale). This slice is additive/no-behavior-change: these
tables exist and are exercised in the local/degenerate mode (peer_platform_id IS NULL)
that api/routes/cascades.py::create_cascade already uses when there is no real peer -
see test_cascades.py for the create/delete cascade coverage that already exercises
this path end-to-end. This file covers the model-level constraints and the
decommission-cleanup fan-out that test_nodes.py's simpler lifecycle test doesn't.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.models.cascade import CascadeExitLink
from dist_vpn_platform.models.exit_link_allocation import ExitLinkAllocation, ExitLinkAllocationStatus
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.models.peer_platform import PeerPlatform, PeerPlatformStatus
from dist_vpn_platform.models.remote_exit_candidate import RemoteExitCandidate, RemoteExitCandidateStatus
from tests.grpc_test_helpers import FakeAgent


async def _login(client: AsyncClient, seeded_admin: dict) -> str:
    response = await client.post(
        "/auth/login", json={"username": seeded_admin["username"], "password": seeded_admin["password"]}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_peer_platform_round_trip(db_engine: None) -> None:
    async with session_scope() as db:
        peer = PeerPlatform(
            label="foreign-platform",
            grpc_address="203.0.113.20:50053",
            status=PeerPlatformStatus.UNKNOWN,
        )
        db.add(peer)
        await db.flush()
        peer_id = peer.id

    async with session_scope() as db:
        fetched = await db.get(PeerPlatform, peer_id)
        assert fetched.label == "foreign-platform"
        assert fetched.grpc_address == "203.0.113.20:50053"
        assert fetched.status == PeerPlatformStatus.UNKNOWN
        assert fetched.peer_cert_pem is None  # not yet exchanged, per module docstring


@pytest.mark.asyncio
async def test_remote_exit_candidate_unique_per_real_peer(db_engine: None) -> None:
    """The UniqueConstraint IS enforced once a real peer_platform_id is set - see
    models/remote_exit_candidate.py's docstring for why the same is NOT true (by SQL
    NULL semantics) for the peer_platform_id IS NULL/local case."""
    async with session_scope() as db:
        peer = PeerPlatform(label="foreign-platform")
        db.add(peer)
        await db.flush()
        peer_id = peer.id

        db.add(RemoteExitCandidate(peer_platform_id=peer_id, remote_node_id="remote-node-1"))
        await db.flush()

    with pytest.raises(IntegrityError):
        async with session_scope() as db:
            db.add(RemoteExitCandidate(peer_platform_id=peer_id, remote_node_id="remote-node-1"))
            await db.flush()


@pytest.mark.asyncio
async def test_exit_link_allocation_round_trip(db_engine: None) -> None:
    async with session_scope() as db:
        exit_node = Node(label="exit-01", role=NodeRole.EXIT, status=NodeStatus.HEALTHY)
        db.add(exit_node)
        await db.flush()

        allocation = ExitLinkAllocation(
            peer_platform_id=None,
            exit_node_id=exit_node.id,
            remote_cascade_id="some-remote-cascade-id",
            hopb_reality_private_key="priv",
            hopb_reality_public_key="pub",
            hopb_reality_short_id="short",
        )
        db.add(allocation)
        await db.flush()
        allocation_id = allocation.id

    async with session_scope() as db:
        fetched = await db.get(ExitLinkAllocation, allocation_id)
        assert fetched.status == ExitLinkAllocationStatus.PENDING  # default
        assert fetched.active_preset == "p1"  # default


@pytest.mark.asyncio
async def test_cascade_out_exposes_per_link_status_and_preset(client: AsyncClient, seeded_admin: dict) -> None:
    """CascadeOut.exit_links (additive alongside the older exit_node_ids) must reflect
    per-link status/preset - see schemas/cascades.py CascadeExitLinkOut's docstring for
    why exit_node_ids alone isn't enough once links can be mid-negotiation."""
    entry_agent = FakeAgent()
    exit_agent = FakeAgent()
    try:
        entry_node_id = await entry_agent.connect(NodeRole.ENTRY)
        exit_node_id = await exit_agent.connect(NodeRole.EXIT)
        token = await _login(client, seeded_admin)

        response = await client.post(
            "/cascades",
            json={"entry_node_id": entry_node_id, "exit_node_ids": [exit_node_id]},
            headers=_auth_headers(token),
        )
        assert response.status_code == 201
        body = response.json()

        assert len(body["exit_links"]) == 1
        link = body["exit_links"][0]
        assert link["remote_node_id"] == exit_node_id
        assert link["status"] == "up"
        assert link["active_preset"] == "p1"
        assert link["remote_exit_candidate_id"]
    finally:
        await entry_agent.close()
        await exit_agent.close()


@pytest.mark.asyncio
async def test_decommission_exit_node_cleans_up_links_allocations_and_candidate(
    client: AsyncClient, seeded_admin: dict
) -> None:
    """Extends test_nodes.py's simpler lifecycle test: decommissioning an exit node
    must also clean up the new RemoteExitCandidate/ExitLinkAllocation rows this
    slice introduced, not just the CascadeExitLink row the old CascadeExitNode
    cleanup already handled (api/routes/nodes.py::decommission_node)."""
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

        delete_response = await client.delete(f"/nodes/{exit_node_id}", headers=_auth_headers(token))
        assert delete_response.status_code == 202

        async with session_scope() as db:
            candidate = (
                await db.execute(
                    select(RemoteExitCandidate).where(
                        RemoteExitCandidate.peer_platform_id.is_(None),
                        RemoteExitCandidate.remote_node_id == exit_node_id,
                    )
                )
            ).scalar_one_or_none()
            assert candidate is None  # cleaned up along with the node

            remaining_links = (
                await db.execute(select(CascadeExitLink).where(CascadeExitLink.cascade_id == cascade_id))
            ).scalars().all()
            assert remaining_links == []

            remaining_allocations = (
                await db.execute(
                    select(ExitLinkAllocation).where(ExitLinkAllocation.exit_node_id == exit_node_id)
                )
            ).scalars().all()
            assert remaining_allocations == []

        # The cascade itself still exists (only its exit-candidate pool changed) -
        # matches the pre-existing CascadeExitNode cleanup's own scope (see
        # api/routes/nodes.py decommission_node's docstring: entry-side reconfiguration
        # is a separate, not-yet-built concern).
        get_response = await client.get(f"/cascades/{cascade_id}", headers=_auth_headers(token))
        assert get_response.status_code == 200
        assert get_response.json()["exit_node_ids"] == []
    finally:
        await entry_agent.close()
        await exit_agent.close()
