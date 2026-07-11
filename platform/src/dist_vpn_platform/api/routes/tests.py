"""GET /tests/{test_id} - contracts/rest/openapi.yaml. Depends on cascade/test-dispatch
work being done first - see api/routes/cascades.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.models.admin_account import AdminAccount

router = APIRouter(prefix="/tests", tags=["tests"])


@router.get("/{test_id}")
async def get_test_result(test_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO(command-dispatch slice): read the LinkTest row once tests can actually be
    # started (see api/routes/cascades.py POST /cascades/{id}/test).
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "Link test retrieval is not implemented in this slice (TZ-5 §4.3 - follow-up slice).",
    )
