"""In-process pub/sub broadcast for `WS /events` (TZ-5 §4.3, §7).

Every currently-connected WebSocket subscriber (api/routes/events.py) gets its own
`asyncio.Queue`; `publish()` fans a JSON-serializable message out to all of them
(non-blocking - a slow/stuck subscriber cannot backpressure the publisher, see
`_MAX_QUEUE_SIZE` below). This is deliberately single-process, in-memory - the same
limitation (and rationale) as `grpc_server/servicer.py::SessionRegistry`: a Platform
process only ever broadcasts events it itself observed (heartbeats landing on its own
gRPC server, LinkTest resolutions it itself dispatched). Running multiple Platform
replicas behind a load balancer would need a shared broker (Redis pub/sub is the
obvious candidate, matching the ARQ/Redis dependency already anticipated elsewhere in
this codebase) so every replica's WebSocket subscribers see every event regardless of
which replica handled the underlying gRPC/HTTP call - out of scope for this slice, see
platform/README.md TODO.

Message envelope matches contracts/rest/openapi.yaml's bottom-of-file WS description:
JSON wrapping one of HealthEvent / Cascade / LinkTest. All three are published now
(`type: "health_event"`, `type: "cascade"`, `type: "link_test"`) - `type: "cascade"`
was a documented gap until Admin Client work found the real consequence: a newly
created cascade never appeared on the live topology graph at all, because the
health_event published alongside it carries no entry_node_id/exit_node_ids for the
client to reconstruct the new cascade's shape from - only a real Cascade snapshot
does. See publish_cascade_event() below for every call site that now sends one.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dist_vpn_platform.models.health_event import HealthEvent
    from dist_vpn_platform.models.link_test import LinkTest
    from dist_vpn_platform.schemas.cascades import CascadeOut

logger = logging.getLogger(__name__)

# Bound each subscriber's queue so one WebSocket connection that never reads (a dead/
# wedged Admin Client tab) cannot grow unbounded memory - it just starts silently
# dropping the oldest backlog for that one subscriber instead. Generous enough that a
# realistic burst (e.g. a cascade's worth of heartbeats) never hits it in practice.
_MAX_QUEUE_SIZE = 1000


class EventBus:
    """Minimal in-process broadcast bus - see module docstring."""

    def __init__(self) -> None:
        self._subscribers: set["asyncio.Queue[dict[str, Any]]"] = set()

    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        self._subscribers.discard(queue)

    def publish(self, message: dict[str, Any]) -> None:
        """Fan `message` out to every currently-subscribed queue. Synchronous/non-
        blocking by design (uses `put_nowait`) so a publisher (e.g. the gRPC heartbeat
        handler) never stalls waiting on a slow WebSocket client - a subscriber whose
        queue is already full just drops this message for itself (logged), everyone
        else still gets it."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("event_bus_subscriber_queue_full", extra={"message_type": message.get("type")})

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


event_bus = EventBus()


def publish_health_event(event: "HealthEvent") -> None:
    """Broadcast a just-flushed/committed HealthEvent row (see grpc_server/servicer.py
    _handle_heartbeat, api/routes/nodes.py decommission_node, api/routes/cascades.py
    create_cascade - every site that writes a HealthEvent row calls this right after)
    to every WS /events subscriber. Reuses HealthEventOut (the same schema GET
    /cascades/{id}/events would return) so the wire format is identical whether a
    client polls or subscribes."""
    from dist_vpn_platform.schemas.health_events import HealthEventOut

    event_bus.publish({"type": "health_event", "data": HealthEventOut.model_validate(event).model_dump(mode="json")})


def publish_cascade_event(cascade_out: "CascadeOut") -> None:
    """Broadcast a full Cascade snapshot - not just the HealthEvent row logged
    alongside a status change - to every WS /events subscriber. Call sites: cascade
    creation (api/routes/cascades.py::create_cascade), a cascade going DOWN by
    heartbeat timeout (services/health_scheduler.py::check_node_timeouts), and a
    cascade recovering back to UP when its entry node's heartbeat resumes
    (grpc_server/servicer.py::_handle_heartbeat) - every place Cascade.status
    actually changes, publish both the HealthEvent (history/audit trail) and this
    (so a live topology graph can render the change without a reload/poll).

    Takes an already-built CascadeOut, not a raw Cascade ORM object, deliberately -
    every call site already needs one for its own HTTP response or already loaded
    Cascade.exit_links eagerly for another reason, so building it once and reusing it
    here avoids a second query and any lazy-load-in-async-context risk."""
    event_bus.publish({"type": "cascade", "data": cascade_out.model_dump(mode="json")})


def publish_link_test_resolved(link_test: "LinkTest") -> None:
    """Broadcast a LinkTest row that just transitioned from pending to resolved (see
    services/link_tests.py::_resolve(), the background continuation of
    POST /cascades/{id}/test - api/routes/cascades.py::start_cascade_test) - TZ-5
    §4.3's "push, not just poll" requirement for test-request results."""
    from dist_vpn_platform.schemas.tests import LinkTestOut

    event_bus.publish({"type": "link_test", "data": LinkTestOut.model_validate(link_test).model_dump(mode="json")})
