"""GET/POST /users, GET/DELETE /users/{user_id} - contracts/rest/openapi.yaml.

User management (TZ-5 §4.4) is now implemented: POST creates a User row (server-
generated uuid/id - the request body never supplies either) and dispatches AddUser to
every cascade's entry-node Agent the user is granted access to; DELETE dispatches
RemoveUser to the same set and marks the user revoked. Dispatch is best-effort
(api/command_dispatch.py) - see api/routes/cascades.py's docstring for the rationale
(an offline Agent doesn't block the Platform-side state change).

The `email` field Xray sees for a given user (AddUser.email / RemoveUser.email) is
always `User.uuid` - the same uuid never changes across secret rotations (TZ-5 §4.4
"UUID пользователя не меняется при ротации серверных секретов"), and using it as the
`email`/stats key too gives telemetry ingestion (grpc_server/servicer.py
`_handle_telemetry`) a stable join key back to this User row without inventing a
second identifier.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from dist_vpn_platform.api.command_dispatch import dispatch_best_effort
from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.db import get_db
from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.grpc_server.servicer import new_command_id
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.models.cascade import Cascade
from dist_vpn_platform.models.user import User, UserCascade
from dist_vpn_platform.schemas.users import UserCreateRequest, UserListResponse, UserOut
from dist_vpn_platform.services import xray_templates as xray

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("dist_vpn_platform.audit")

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=UserListResponse)
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> UserListResponse:
    total = (await db.execute(select(func.count()).select_from(User))).scalar_one()

    stmt = (
        select(User)
        .options(selectinload(User.cascade_links))
        .order_by(User.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()

    return UserListResponse(
        items=[
            UserOut(
                id=u.id,
                name=u.name,
                uuid=u.uuid,
                cascade_ids=u.cascade_ids,
                created_at=u.created_at,
                revoked_at=u.revoked_at,
            )
            for u in items
        ],
        total=total,
    )


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> UserOut:
    cascades: list[Cascade] = []
    for cascade_id in body.cascade_ids:
        cascade = await db.get(Cascade, cascade_id)
        if cascade is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"cascade not found: {cascade_id}")
        cascades.append(cascade)

    # id and uuid are both server-generated (models/user.py `default=new_uuid` on both
    # columns) - the request body only ever supplies `name`/`cascade_ids`.
    user = User(name=body.name)
    db.add(user)
    await db.flush()

    for cascade in cascades:
        db.add(UserCascade(user_id=user.id, cascade_id=cascade.id))
    await db.flush()

    for cascade in cascades:
        await dispatch_best_effort(
            cascade.entry_node_id,
            pb.Command(
                command_id=new_command_id(),
                add_user=pb.AddUser(
                    inbound_tag=xray.hopa_inbound_tag(cascade.id),
                    uuid=user.uuid,
                    email=user.uuid,
                    # No confirmed preset->flow mapping survived (see
                    # services/xray_templates.py provenance note) - left empty per the
                    # proto's own comment ("пусто, если пресету не требуется flow").
                    flow="",
                ),
            ),
            context=f"user {user.id} add to cascade {cascade.id}",
        )

    await db.commit()

    audit_logger.info(
        "user_created", extra={"admin_id": admin.id, "user_id": user.id, "cascade_ids": body.cascade_ids}
    )

    return UserOut(
        id=user.id,
        name=user.name,
        uuid=user.uuid,
        cascade_ids=[c.id for c in cascades],
        created_at=user.created_at,
        revoked_at=None,
    )


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> UserOut:
    stmt = select(User).options(selectinload(User.cascade_links)).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    return UserOut(
        id=user.id,
        name=user.name,
        uuid=user.uuid,
        cascade_ids=user.cascade_ids,
        created_at=user.created_at,
        revoked_at=user.revoked_at,
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> None:
    stmt = select(User).options(selectinload(User.cascade_links)).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if user.revoked_at is not None:
        return  # already revoked - idempotent

    cascade_ids = user.cascade_ids
    for cascade_id in cascade_ids:
        cascade = await db.get(Cascade, cascade_id)
        if cascade is None:
            continue
        await dispatch_best_effort(
            cascade.entry_node_id,
            pb.Command(
                command_id=new_command_id(),
                remove_user=pb.RemoveUser(inbound_tag=xray.hopa_inbound_tag(cascade.id), email=user.uuid),
            ),
            context=f"user {user.id} remove from cascade {cascade.id}",
        )

    await db.execute(delete(UserCascade).where(UserCascade.user_id == user_id))
    user.revoked_at = datetime.now(timezone.utc)
    await db.commit()

    audit_logger.info("user_revoked", extra={"admin_id": admin.id, "user_id": user_id})
