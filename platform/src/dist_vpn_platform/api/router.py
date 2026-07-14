"""Aggregates all route modules under the /api/v1 prefix (contracts/rest/openapi.yaml
`servers: [{url: /api/v1}]`)."""
from __future__ import annotations

from fastapi import APIRouter

from dist_vpn_platform.api.routes import auth, cascades, events, nodes, tests, usage, users

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(nodes.router)
api_router.include_router(cascades.router)
api_router.include_router(users.router)
api_router.include_router(usage.router)
api_router.include_router(tests.router)
api_router.include_router(events.router)
