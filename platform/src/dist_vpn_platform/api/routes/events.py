"""WS /events - contracts/rest/openapi.yaml (described textually at the bottom of the
file, no AsyncAPI schema yet).

Real-time push (TZ-5 §4.3: "push, not only by request - Admin Client should update
without a manual page refresh"): every connected client is subscribed to
services/event_bus.py's in-process broadcast bus and receives, as JSON, every
HealthEvent created (node/cascade state transitions - see event_bus.py's
publish_health_event() call sites: grpc_server/servicer.py heartbeat handling,
api/routes/nodes.py decommission, api/routes/cascades.py cascade creation), every full
Cascade snapshot published on creation/DOWN/recovery (publish_cascade_event() - added
after Admin Client work found a health_event alone doesn't carry enough for a client
to render a brand-new cascade on a live topology graph), and every LinkTest that
resolves (services/link_tests.py::_resolve(), the follow-up to POST /cascades/{id}/test
- see api/routes/cascades.py::start_cascade_test).

Single Platform process only - see event_bus.py's module docstring for the documented
multi-replica follow-up (Redis pub/sub).

The handler below runs two concurrent loops rather than one: a *sender* draining
`event_bus`'s per-connection queue, and a *watcher* whose only job is to notice the
client going away. This route never expects any inbound message from the Admin
Client - but without something reading the receive side of the socket at all, this
handler would have no way to learn a client disconnected while it's parked on
`queue.get()` waiting for the next event (no exception, no signal - `send_json()` only
fails once the handler *tries* to send again, which may be arbitrarily far in the
future, or never, for a quiet cascade). That leaves the subscription in
`event_bus`'s subscriber set forever - a slow, unbounded leak of queues/tasks across
the Platform process's lifetime as Admin Client tabs open and close. The watcher's
`websocket.receive()` surfaces the disconnect immediately instead.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from dist_vpn_platform.security.tokens import TokenError, TokenType, decode_token
from dist_vpn_platform.services.event_bus import event_bus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


@router.websocket("/events")
async def events_stream(websocket: WebSocket) -> None:
    # Bearer token as query param, per the contract note (openapi.yaml bottom-of-file
    # WS description) since browsers can't set arbitrary headers on the WS handshake.
    token = websocket.query_params.get("token")
    try:
        if token is None:
            raise TokenError("missing token")
        decode_token(token, expected_type=TokenType.ACCESS)
    except TokenError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid or missing token")
        return

    await websocket.accept()
    queue = event_bus.subscribe()
    logger.info("ws_events_subscribed", extra={"subscriber_count": event_bus.subscriber_count})

    async def _send_events() -> None:
        while True:
            message = await queue.get()
            await websocket.send_json(message)

    async def _watch_for_disconnect() -> None:
        # No inbound message is ever expected on this route - this loop exists purely
        # to detect a "websocket.disconnect" ASGI event (see module docstring above).
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", status.WS_1000_NORMAL_CLOSURE))

    send_task = asyncio.create_task(_send_events())
    watch_task = asyncio.create_task(_watch_for_disconnect())
    try:
        done, pending = await asyncio.wait({send_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        send_task.cancel()
        watch_task.cancel()
        raise
    finally:
        event_bus.unsubscribe(queue)
        logger.info("ws_events_unsubscribed", extra={"subscriber_count": event_bus.subscriber_count})
