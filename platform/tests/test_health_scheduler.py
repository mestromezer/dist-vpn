"""services/health_scheduler.py - passive auto-detection of DOWN nodes/cascades by
heartbeat timeout (TZ-5 §4.3). Calls check_node_timeouts() directly (one pass), the
same "exercise the logic directly, the loop/sleep wrapper around it is not what needs
proving" approach used elsewhere in this test suite - see run_health_scheduler_forever
for the thin scheduling wrapper this deliberately doesn't re-test."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from dist_vpn_platform.config import settings
from dist_vpn_platform.db import session_scope
from dist_vpn_platform.models.cascade import Cascade, CascadeStatus
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.services.health_scheduler import check_node_timeouts


def _stale_timestamp() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=settings.node_heartbeat_timeout_seconds + 5)


def _fresh_timestamp() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_stale_healthy_node_is_marked_down(db_engine: None) -> None:
    async with session_scope() as db:
        node = Node(
            label="stale-entry",
            role=NodeRole.ENTRY,
            status=NodeStatus.HEALTHY,
            last_heartbeat_at=_stale_timestamp(),
        )
        db.add(node)
        await db.flush()
        node_id = node.id

    events = await check_node_timeouts()

    async with session_scope() as db:
        node = await db.get(Node, node_id)
        assert node.status == NodeStatus.DOWN

        rows = (
            await db.execute(select(HealthEvent).where(HealthEvent.node_id == node_id))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].from_status == "healthy"
        assert rows[0].to_status == "down"

    assert any(e.node_id == node_id for e in events)


@pytest.mark.asyncio
async def test_fresh_heartbeat_is_not_marked_down(db_engine: None) -> None:
    async with session_scope() as db:
        node = Node(
            label="fresh-entry",
            role=NodeRole.ENTRY,
            status=NodeStatus.HEALTHY,
            last_heartbeat_at=_fresh_timestamp(),
        )
        db.add(node)
        await db.flush()
        node_id = node.id

    await check_node_timeouts()

    async with session_scope() as db:
        node = await db.get(Node, node_id)
        assert node.status == NodeStatus.HEALTHY


@pytest.mark.asyncio
async def test_node_that_never_sent_a_heartbeat_is_not_marked_down(db_engine: None) -> None:
    """ENROLLING nodes with last_heartbeat_at=NULL must not be swept up - see
    check_node_timeouts' own docstring for why "never heartbeated" isn't the same
    kind of fact as "went silent after being healthy"."""
    async with session_scope() as db:
        node = Node(label="never-heartbeated", role=NodeRole.ENTRY, status=NodeStatus.ENROLLING)
        db.add(node)
        await db.flush()
        node_id = node.id

    await check_node_timeouts()

    async with session_scope() as db:
        node = await db.get(Node, node_id)
        assert node.status == NodeStatus.ENROLLING


@pytest.mark.asyncio
async def test_already_down_node_is_not_re_flagged(db_engine: None) -> None:
    async with session_scope() as db:
        node = Node(
            label="already-down",
            role=NodeRole.ENTRY,
            status=NodeStatus.DOWN,
            last_heartbeat_at=_stale_timestamp(),
        )
        db.add(node)
        await db.flush()
        node_id = node.id

    events = await check_node_timeouts()

    assert not any(e.node_id == node_id for e in events)


@pytest.mark.asyncio
async def test_stale_entry_node_marks_its_cascades_down_too(db_engine: None) -> None:
    async with session_scope() as db:
        entry = Node(
            label="stale-entry-2",
            role=NodeRole.ENTRY,
            status=NodeStatus.HEALTHY,
            last_heartbeat_at=_stale_timestamp(),
        )
        db.add(entry)
        await db.flush()

        up_cascade = Cascade(entry_node_id=entry.id, status=CascadeStatus.UP)
        already_down_cascade = Cascade(entry_node_id=entry.id, status=CascadeStatus.DOWN)
        db.add(up_cascade)
        db.add(already_down_cascade)
        await db.flush()
        entry_id, up_cascade_id, already_down_id = entry.id, up_cascade.id, already_down_cascade.id

    events = await check_node_timeouts()

    async with session_scope() as db:
        up_cascade = await db.get(Cascade, up_cascade_id)
        assert up_cascade.status == CascadeStatus.DOWN

    cascade_event_ids = {e.cascade_id for e in events if e.cascade_id}
    assert up_cascade_id in cascade_event_ids
    # Already-DOWN cascade must not get a redundant/duplicate transition event.
    assert already_down_id not in cascade_event_ids


@pytest.mark.asyncio
async def test_stale_exit_node_does_not_mark_cascades_down(db_engine: None) -> None:
    """Hop B failover is already handled natively by Xray's own balancer
    (leastPing across exit candidates) - an exit node going silent is not the same
    kind of failure as an entry node going silent, and must not cascade into
    marking every cascade that merely lists it as an exit candidate DOWN."""
    async with session_scope() as db:
        entry = Node(label="healthy-entry", role=NodeRole.ENTRY, status=NodeStatus.HEALTHY, last_heartbeat_at=_fresh_timestamp())
        exit_node = Node(
            label="stale-exit", role=NodeRole.EXIT, status=NodeStatus.HEALTHY, last_heartbeat_at=_stale_timestamp()
        )
        db.add(entry)
        db.add(exit_node)
        await db.flush()

        cascade = Cascade(entry_node_id=entry.id, status=CascadeStatus.UP)
        db.add(cascade)
        await db.flush()
        cascade_id, exit_node_id = cascade.id, exit_node.id

    events = await check_node_timeouts()

    async with session_scope() as db:
        cascade = await db.get(Cascade, cascade_id)
        assert cascade.status == CascadeStatus.UP  # unaffected by the exit node's own timeout

        exit_node = await db.get(Node, exit_node_id)
        assert exit_node.status == NodeStatus.DOWN  # the exit node itself is still correctly flagged

    assert not any(e.cascade_id == cascade_id for e in events)
