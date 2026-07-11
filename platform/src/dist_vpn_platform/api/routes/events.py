"""WS /events - contracts/rest/openapi.yaml (described textually at the bottom of the
file, no AsyncAPI schema yet).

Real-time push of HealthEvent/Cascade/LinkTest updates (TZ-5 §4.3) needs the
cascade-assembly and command-dispatch work to exist first. This slice wires up the
route and the bearer-token-as-query-param auth shape so the Admin Client can point at
a stable URL, but immediately closes with a clear reason - see platform/README.md TODO.
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from dist_vpn_platform.security.tokens import TokenError, TokenType, decode_token

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
    try:
        # TODO(cascade-assembly / command-dispatch slice): subscribe this connection to
        # an in-process (or Redis pub/sub, once ARQ/Redis is introduced) broadcast of
        # HealthEvent/Cascade/LinkTest updates instead of closing immediately.
        await websocket.send_json(
            {
                "type": "not_implemented",
                "detail": "Real-time event push is not implemented in this slice (TZ-5 §4.3 - follow-up slice).",
            }
        )
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
    except WebSocketDisconnect:
        pass
