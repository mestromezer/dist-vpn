"""GET /usage - contracts/rest/openapi.yaml. Deferred until TelemetryReport ingestion
(gRPC servicer TODO, see grpc_server/servicer.py) actually populates UsageStat rows."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.models.admin_account import AdminAccount

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("")
async def get_usage(
    user_id: str | None = Query(default=None),
    node_id: str | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    _admin: AdminAccount = Depends(require_admin),
) -> None:
    # TODO(telemetry-ingestion slice): aggregate UsageStat rows populated from
    # AgentMessage.TelemetryReport (TZ-5 §4.4, §6.4 - byte counters only).
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "Usage aggregation is not implemented in this slice (TZ-5 §4.4 - follow-up slice).",
    )
