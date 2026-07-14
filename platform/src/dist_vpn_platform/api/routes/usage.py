"""GET /usage - contracts/rest/openapi.yaml.

UsageStat rows are already the aggregate (grpc_server/servicer.py::_handle_telemetry
upserts-by-add into hourly period_start buckets per scope/key/node, rather than one row
per TelemetryReport) - this endpoint is a filtered read over that existing
pre-aggregation, not a second aggregation layer. Bare array response, per the
contract's own original shape (matches GET /cascades/{id}/events - see that route's
docstring for why this file doesn't invent an items+total envelope here either).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.db import get_db
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.models.usage_stat import UsageStat
from dist_vpn_platform.schemas.usage import UsageStatOut

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("", response_model=list[UsageStatOut])
async def get_usage(
    user_id: str | None = Query(default=None),
    node_id: str | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> list[UsageStatOut]:
    """Filters (all optional, combinable): user_id/node_id match exactly;
    from_/to bound period_start (inclusive on both ends). No `scope` filter in the
    contract's own parameter list, so this returns rows across every scope
    ("user"/"inbound"/"outbound") that match the other filters - not just
    scope=="user" rows, even though UsageStatOut.user_id is only populated for those
    (None for inbound/outbound scope rows, same as the underlying UsageStat column)."""
    stmt = select(UsageStat).order_by(UsageStat.period_start.desc())
    if user_id is not None:
        stmt = stmt.where(UsageStat.user_id == user_id)
    if node_id is not None:
        stmt = stmt.where(UsageStat.node_id == node_id)
    if from_ is not None:
        stmt = stmt.where(UsageStat.period_start >= from_)
    if to is not None:
        stmt = stmt.where(UsageStat.period_start <= to)

    items = (await db.execute(stmt)).scalars().all()
    return [UsageStatOut.model_validate(item) for item in items]
