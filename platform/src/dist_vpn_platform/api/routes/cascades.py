"""GET/POST/PATCH/DELETE /cascades, GET /cascades/{id}/events, POST /cascades/{id}/test
- contracts/rest/openapi.yaml.

Cascade assembly / failover reconfiguration (TZ-5 §4.2) is out of scope for this
api-skeleton slice - see platform/README.md "Scope of this slice / TODO". Routes are
still declared (not omitted) so the contract shape is visible to the Admin Client
team from day one, each returning 501 with a clear TODO.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.schemas.cascades import CascadeInput

router = APIRouter(prefix="/cascades", tags=["cascades"])

_NOT_IMPLEMENTED = "Cascade assembly is not implemented in this slice (TZ-5 §4.2 - follow-up slice)."


@router.get("")
async def list_cascades(_admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO(cascade-assembly slice): list Cascade rows with current CascadeStatus.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_cascade(body: CascadeInput, _admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO(cascade-assembly slice): persist Cascade+CascadeExitNode rows, dispatch
    # AddOutbound/AddInbound/OverrideBalancer Commands to the affected Agents (TZ-5 §4.2).
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.get("/{cascade_id}")
async def get_cascade(cascade_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.patch("/{cascade_id}")
async def patch_cascade(
    cascade_id: str, body: CascadeInput, _admin: AdminAccount = Depends(require_admin)
) -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.delete("/{cascade_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cascade(cascade_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.get("/{cascade_id}/events")
async def list_cascade_events(cascade_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO: this one is cheap to implement against the existing HealthEvent table -
    # deferred only because it's most useful once cascades actually exist (see above).
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.post("/{cascade_id}/test", status_code=status.HTTP_202_ACCEPTED)
async def start_cascade_test(cascade_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO(command-dispatch slice): send Command{RunTest} over the gRPC session to the
    # cascade's entry-node Agent, correlate the async TestResult by test_id (TZ-5 §4.3).
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)
