"""GET/POST /users, GET/DELETE /users/{user_id} - contracts/rest/openapi.yaml.

User management (TZ-5 §4.4) fans out AddUser/RemoveUser commands to affected Agents,
which needs cascade membership (api/routes/cascades.py) to exist first - out of scope
for this api-skeleton slice, see platform/README.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.schemas.users import UserCreateRequest

router = APIRouter(prefix="/users", tags=["users"])

_NOT_IMPLEMENTED = "User management is not implemented in this slice (TZ-5 §4.4 - follow-up slice)."


@router.get("")
async def list_users(_admin: AdminAccount = Depends(require_admin)) -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreateRequest, _admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO(user-management slice): create User row, dispatch AddUser Command with the
    # same UUID to every Agent behind body.cascade_ids (TZ-5 §4.4).
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.get("/{user_id}")
async def get_user(user_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_user(user_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO(user-management slice): dispatch RemoveUser Command to all affected Agents.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)
