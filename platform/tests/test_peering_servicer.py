"""PlatformPeeringServicer (grpc_server/peering_servicer.py) - driven directly, same
"exercise the servicer logic, the real gRPC transport is grpcio-tools boilerplate
already covered by test_mtls_integration.py" approach as tests/grpc_test_helpers.py
uses for AgentControlServicer. None of these RPC methods touch `context` (no
context.abort()/set_code() calls - every outcome is expressed as a response field,
e.g. accepted=False), so passing None for it is sufficient here.
"""
from __future__ import annotations

import pytest

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import platform_peering_pb2 as pb
from dist_vpn_platform.grpc_server.peering_servicer import PlatformPeeringServicer
from dist_vpn_platform.models.exit_link_allocation import ExitLinkAllocation, ExitLinkAllocationStatus
from dist_vpn_platform.models.node import NodeRole, NodeStatus
from dist_vpn_platform.models.peer_platform import PeerPlatform
from tests.grpc_test_helpers import FakeAgent


async def _make_peer() -> str:
    async with session_scope() as db:
        peer = PeerPlatform(label="ru-platform")
        db.add(peer)
        await db.flush()
        return peer.id


@pytest.mark.asyncio
async def test_provision_exit_link_unknown_node_is_rejected(db_engine: None) -> None:
    peer_id = await _make_peer()
    servicer = PlatformPeeringServicer(peer_platform_id=peer_id)

    response = await servicer.ProvisionExitLink(
        pb.ProvisionExitLinkRequest(remote_cascade_id="remote-cascade-1", exit_node_id="does-not-exist"),
        None,
    )
    assert response.accepted is False
    assert "unknown exit_node_id" in response.reason


@pytest.mark.asyncio
async def test_provision_exit_link_creates_allocation_and_dispatches(db_engine: None) -> None:
    peer_id = await _make_peer()
    servicer = PlatformPeeringServicer(peer_platform_id=peer_id)

    exit_agent = FakeAgent()
    try:
        exit_node_id = await exit_agent.connect(NodeRole.EXIT)

        response = await servicer.ProvisionExitLink(
            pb.ProvisionExitLinkRequest(
                remote_cascade_id="remote-cascade-1", exit_node_id=exit_node_id, initial_preset="p1"
            ),
            None,
        )
        assert response.accepted is True
        assert response.link_id
        assert response.hopb_reality_public_key
        assert response.hopb_reality_short_id

        exit_ops = [c.WhichOneof("op") for c in exit_agent.received_commands]
        assert exit_ops == ["add_inbound"]

        async with session_scope() as db:
            allocation = await db.get(ExitLinkAllocation, response.link_id)
            assert allocation.peer_platform_id == peer_id
            assert allocation.exit_node_id == exit_node_id
            assert allocation.remote_cascade_id == "remote-cascade-1"
            assert allocation.status == ExitLinkAllocationStatus.UP
            assert allocation.hopb_reality_private_key  # never leaves this Platform - see model docstring
    finally:
        await exit_agent.close()


@pytest.mark.asyncio
async def test_provision_exit_link_is_idempotent(db_engine: None) -> None:
    peer_id = await _make_peer()
    servicer = PlatformPeeringServicer(peer_platform_id=peer_id)

    exit_agent = FakeAgent()
    try:
        exit_node_id = await exit_agent.connect(NodeRole.EXIT)
        request = pb.ProvisionExitLinkRequest(
            remote_cascade_id="remote-cascade-1", exit_node_id=exit_node_id, initial_preset="p1"
        )

        first = await servicer.ProvisionExitLink(request, None)
        second = await servicer.ProvisionExitLink(request, None)

        assert first.link_id == second.link_id
        # Second call must not re-dispatch (idempotent re-provision, see servicer docstring).
        add_inbound_count = sum(1 for c in exit_agent.received_commands if c.WhichOneof("op") == "add_inbound")
        assert add_inbound_count == 1
    finally:
        await exit_agent.close()


@pytest.mark.asyncio
async def test_provision_exit_link_rejects_decommissioned_node(db_engine: None) -> None:
    """DOWN/ENROLLING are still attempted (dispatch result, not the cached
    Node.status, decides the allocation's own status - see servicer's comment) -
    only DECOMMISSIONED is a hard, upfront reject."""
    peer_id = await _make_peer()
    servicer = PlatformPeeringServicer(peer_platform_id=peer_id)

    from dist_vpn_platform.models.node import Node

    async with session_scope() as db:
        node = Node(label="gone-exit", role=NodeRole.EXIT, status=NodeStatus.DECOMMISSIONED)
        db.add(node)
        await db.flush()
        node_id = node.id

    response = await servicer.ProvisionExitLink(
        pb.ProvisionExitLinkRequest(remote_cascade_id="remote-cascade-1", exit_node_id=node_id, initial_preset="p1"),
        None,
    )
    assert response.accepted is False
    assert "not usable" in response.reason


@pytest.mark.asyncio
async def test_release_exit_link_dispatches_remove_and_deletes_row(db_engine: None) -> None:
    peer_id = await _make_peer()
    servicer = PlatformPeeringServicer(peer_platform_id=peer_id)

    exit_agent = FakeAgent()
    try:
        exit_node_id = await exit_agent.connect(NodeRole.EXIT)
        provision = await servicer.ProvisionExitLink(
            pb.ProvisionExitLinkRequest(remote_cascade_id="remote-cascade-1", exit_node_id=exit_node_id), None
        )
        exit_agent.received_commands.clear()

        response = await servicer.ReleaseExitLink(pb.ReleaseExitLinkRequest(link_id=provision.link_id), None)
        assert response.released is True

        exit_ops = [c.WhichOneof("op") for c in exit_agent.received_commands]
        assert exit_ops == ["remove_inbound"]

        async with session_scope() as db:
            assert await db.get(ExitLinkAllocation, provision.link_id) is None
    finally:
        await exit_agent.close()


@pytest.mark.asyncio
async def test_release_unknown_link_is_a_noop_not_an_error(db_engine: None) -> None:
    peer_id = await _make_peer()
    servicer = PlatformPeeringServicer(peer_platform_id=peer_id)

    response = await servicer.ReleaseExitLink(pb.ReleaseExitLinkRequest(link_id="does-not-exist"), None)
    assert response.released is False


@pytest.mark.asyncio
async def test_switch_link_preset_redispatches_with_fresh_keys(db_engine: None) -> None:
    peer_id = await _make_peer()
    servicer = PlatformPeeringServicer(peer_platform_id=peer_id)

    exit_agent = FakeAgent()
    try:
        exit_node_id = await exit_agent.connect(NodeRole.EXIT)
        provision = await servicer.ProvisionExitLink(
            pb.ProvisionExitLinkRequest(remote_cascade_id="remote-cascade-1", exit_node_id=exit_node_id), None
        )
        exit_agent.received_commands.clear()

        response = await servicer.SwitchLinkPreset(
            pb.SwitchLinkPresetRequest(link_id=provision.link_id, preset="p2"), None
        )
        assert response.accepted is True
        assert response.hopb_reality_public_key != provision.hopb_reality_public_key  # fresh keys

        exit_ops = [c.WhichOneof("op") for c in exit_agent.received_commands]
        assert exit_ops == ["remove_inbound", "add_inbound"]

        async with session_scope() as db:
            allocation = await db.get(ExitLinkAllocation, provision.link_id)
            assert allocation.active_preset == "p2"
            assert allocation.hopb_reality_public_key == response.hopb_reality_public_key
    finally:
        await exit_agent.close()


@pytest.mark.asyncio
async def test_list_exit_links_scoped_to_peer_and_optional_cascade_filter(db_engine: None) -> None:
    peer_id = await _make_peer()
    other_peer_id = await _make_peer()
    servicer = PlatformPeeringServicer(peer_platform_id=peer_id)
    other_servicer = PlatformPeeringServicer(peer_platform_id=other_peer_id)

    exit_agent_a = FakeAgent()
    exit_agent_b = FakeAgent()
    try:
        exit_a = await exit_agent_a.connect(NodeRole.EXIT)
        exit_b = await exit_agent_b.connect(NodeRole.EXIT)

        await servicer.ProvisionExitLink(
            pb.ProvisionExitLinkRequest(remote_cascade_id="cascade-a", exit_node_id=exit_a), None
        )
        await servicer.ProvisionExitLink(
            pb.ProvisionExitLinkRequest(remote_cascade_id="cascade-b", exit_node_id=exit_b), None
        )
        # A different peer's own link must never show up in this peer's listing.
        await other_servicer.ProvisionExitLink(
            pb.ProvisionExitLinkRequest(remote_cascade_id="cascade-a", exit_node_id=exit_a), None
        )

        all_links = await servicer.ListExitLinks(pb.ListExitLinksRequest(), None)
        assert len(all_links.links) == 2

        filtered = await servicer.ListExitLinks(pb.ListExitLinksRequest(remote_cascade_id="cascade-a"), None)
        assert len(filtered.links) == 1
        assert filtered.links[0].exit_node_id == exit_a
        assert filtered.links[0].status == pb.LINK_HEALTH_STATUS_UP
    finally:
        await exit_agent_a.close()
        await exit_agent_b.close()
