"""GET /tests/{test_id} - contracts/rest/openapi.yaml.

Real, backed by the LinkTest rows POST /cascades/{id}/test (api/routes/cascades.py,
services/link_tests.py) persists - returns whatever the row's current state is,
including still-pending (outcome=null) if the Agent hasn't replied yet.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.db import get_db
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.models.link_test import LinkTest
from dist_vpn_platform.schemas.tests import LinkTestOut

router = APIRouter(prefix="/tests", tags=["tests"])


@router.get("/{test_id}", response_model=LinkTestOut)
async def get_test_result(
    test_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> LinkTestOut:
    link_test = await db.get(LinkTest, test_id)
    if link_test is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link test not found")
    return LinkTestOut.model_validate(link_test)
