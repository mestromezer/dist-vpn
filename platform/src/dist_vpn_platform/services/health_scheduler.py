"""Periodic auto-detection of DOWN nodes/cascades by heartbeat timeout (TZ-5 §4.3).

In-process asyncio background task, not Redis/ARQ - matches this project's existing
single-process reality (SessionRegistry in grpc_server/servicer.py and event_bus.py
already document the same "horizontal scaling needs a shared broker, not built yet"
limitation, see platform/README.md TODO section) - introducing a new infra dependency
(Redis) just for one periodic timestamp check would be overengineering at this
project's current scope, not a correctness requirement.

Everything reactive (a real Heartbeat arriving) was already handled before this module
existed - Node recovering to HEALTHY on the next real heartbeat
(grpc_server/servicer.py::_handle_heartbeat). Cascade recovery (DOWN -> UP once its
entry node's heartbeat resumes) is also handled there, in the same method - added
alongside this module rather than before it, since a DOWN cascade was itself a new
concept this module introduced; there was nothing to recover from previously. What
this module closes is the *passive* side: nothing ever noticed a node that had simply
gone silent (Agent crashed, VDS lost power, network partition) - status only ever
changed in reaction to an incoming message, never from an incoming message's *absence*.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from dist_vpn_platform.config import settings
from dist_vpn_platform.db import session_scope
from dist_vpn_platform.models.cascade import Cascade, CascadeStatus
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.schemas.cascades import CascadeOut
from dist_vpn_platform.services.event_bus import publish_cascade_event, publish_health_event

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class TimeoutCheckResult:
    events: list[HealthEvent]
    down_cascades: list[CascadeOut]


async def check_node_timeouts() -> TimeoutCheckResult:
    """One pass: mark any node whose last_heartbeat_at is older than
    settings.node_heartbeat_timeout_seconds as DOWN, and any cascade whose entry node
    just went DOWN as DOWN too. Recovery is NOT this module's concern - see the module
    docstring for where that lives.

    ENROLLING nodes are deliberately excluded - a node that has never sent its first
    heartbeat at all hasn't "timed out" in the same sense a previously-healthy node
    going silent has; treating a slow-to-enroll node as a false "timeout" would be
    misleading, not more correct.

    Returns the HealthEvents written this pass (already committed) so the caller can
    publish them to WS /events - same publish-after-commit ordering already used by
    _handle_heartbeat/create_cascade/decommission_node, so a WS subscriber never learns
    about a transition a concurrent rollback could still have discarded - plus a full
    CascadeOut snapshot per newly-DOWN cascade, for the same "a topology graph needs
    more than just the HealthEvent row to render the change" reason documented on
    event_bus.py::publish_cascade_event.
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=settings.node_heartbeat_timeout_seconds)
    events: list[HealthEvent] = []
    down_cascades: list[CascadeOut] = []

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
                        select(Cascade)
                        .options(selectinload(Cascade.exit_links))
                        .where(Cascade.entry_node_id == node.id, Cascade.status != CascadeStatus.DOWN)
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
                    down_cascades.append(
                        CascadeOut(
                            id=cascade.id,
                            entry_node_id=cascade.entry_node_id,
                            exit_node_ids=cascade.exit_node_ids,
                            status=cascade.status,
                            hop_a_presets=cascade.hop_a_presets,
                            hopa_reality_public_key=cascade.hopa_reality_public_key,
                            hopa_reality_short_id=cascade.hopa_reality_short_id,
                        )
                    )

        await db.flush()

    return TimeoutCheckResult(events=events, down_cascades=down_cascades)


async def run_health_scheduler_forever() -> None:
    """Runs check_node_timeouts() every settings.health_scheduler_interval_seconds
    until cancelled - started/cancelled from app.py's lifespan. A single iteration
    raising is logged and swallowed, not fatal to the loop - the same "one bad cycle
    shouldn't kill background monitoring forever" reasoning as the Agent's own
    heartbeat-loop error handling."""
    while True:
        try:
            result = await check_node_timeouts()
            for event in result.events:
                publish_health_event(event)
            for cascade_out in result.down_cascades:
                publish_cascade_event(cascade_out)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("health_scheduler_iteration_failed")
        await asyncio.sleep(settings.health_scheduler_interval_seconds)
