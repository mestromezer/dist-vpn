"""Periodic auto-detection of DOWN nodes/cascades by heartbeat timeout (TZ-5 §4.3).

In-process asyncio background task, not Redis/ARQ - matches this project's existing
single-process reality (SessionRegistry in grpc_server/servicer.py and event_bus.py
already document the same "horizontal scaling needs a shared broker, not built yet"
limitation, see platform/README.md TODO section) - introducing a new infra dependency
(Redis) just for one periodic timestamp check would be overengineering at this
project's current scope, not a correctness requirement.

Everything reactive (a real Heartbeat arriving) was already handled before this module
existed - Node/Cascade recovering to HEALTHY/UP on the next real heartbeat
(grpc_server/servicer.py::_handle_heartbeat). What was missing, and this module closes,
is the *passive* side: nothing ever noticed a node that had simply gone silent (Agent
crashed, VDS lost power, network partition) - status only ever changed in reaction to
an incoming message, never from an incoming message's *absence*.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from dist_vpn_platform.config import settings
from dist_vpn_platform.db import session_scope
from dist_vpn_platform.models.cascade import Cascade, CascadeStatus
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.services.event_bus import publish_health_event

logger = logging.getLogger(__name__)


async def check_node_timeouts() -> list[HealthEvent]:
    """One pass: mark any node whose last_heartbeat_at is older than
    settings.node_heartbeat_timeout_seconds as DOWN, and any cascade whose entry node
    just went DOWN as DOWN too (symmetric with _handle_heartbeat's own reactive
    DOWN/DEGRADED -> HEALTHY recovery transition - that one still fires normally once
    the Agent reconnects and sends a real heartbeat again, this module doesn't touch
    recovery at all, only the missed-heartbeat -> DOWN direction).

    ENROLLING nodes are deliberately excluded - a node that has never sent its first
    heartbeat at all hasn't "timed out" in the same sense a previously-healthy node
    going silent has; treating a slow-to-enroll node as a false "timeout" would be
    misleading, not more correct.

    Returns the HealthEvents written this pass (already committed) so the caller can
    publish them to WS /events - same publish-after-commit ordering already used by
    _handle_heartbeat/create_cascade/decommission_node, so a WS subscriber never learns
    about a transition a concurrent rollback could still have discarded.
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=settings.node_heartbeat_timeout_seconds)
    events: list[HealthEvent] = []

    async with session_scope() as db:
        stale_nodes = (
            await db.execute(
                select(Node).where(
                    Node.last_heartbeat_at.is_not(None),
                    Node.last_heartbeat_at < threshold,
                    Node.status.in_([NodeStatus.HEALTHY, NodeStatus.DEGRADED]),
                )
            )
        ).scalars().all()

        for node in stale_nodes:
            previous_node_status = node.status
            node.status = NodeStatus.DOWN
            node_event = HealthEvent(
                node_id=node.id,
                from_status=previous_node_status.value,
                to_status=NodeStatus.DOWN.value,
                reason=f"no heartbeat for over {settings.node_heartbeat_timeout_seconds}s",
            )
            db.add(node_event)
            events.append(node_event)
            logger.warning(
                "node_heartbeat_timeout",
                extra={"node_id": node.id, "last_heartbeat_at": node.last_heartbeat_at.isoformat()},
            )

            if node.role == NodeRole.ENTRY:
                affected_cascades = (
                    await db.execute(
                        select(Cascade).where(
                            Cascade.entry_node_id == node.id,
                            Cascade.status != CascadeStatus.DOWN,
                        )
                    )
                ).scalars().all()
                for cascade in affected_cascades:
                    previous_cascade_status = cascade.status
                    cascade.status = CascadeStatus.DOWN
                    cascade_event = HealthEvent(
                        cascade_id=cascade.id,
                        from_status=previous_cascade_status.value,
                        to_status=CascadeStatus.DOWN.value,
                        reason=f"entry node {node.id} heartbeat timeout",
                    )
                    db.add(cascade_event)
                    events.append(cascade_event)

        await db.flush()

    return events


async def run_health_scheduler_forever() -> None:
    """Runs check_node_timeouts() every settings.health_scheduler_interval_seconds
    until cancelled - started/cancelled from app.py's lifespan. A single iteration
    raising is logged and swallowed, not fatal to the loop - the same "one bad cycle
    shouldn't kill background monitoring forever" reasoning as the Agent's own
    heartbeat-loop error handling."""
    while True:
        try:
            events = await check_node_timeouts()
            for event in events:
                publish_health_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("health_scheduler_iteration_failed")
        await asyncio.sleep(settings.health_scheduler_interval_seconds)
