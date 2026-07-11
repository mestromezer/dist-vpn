"""GET/POST /nodes, GET/PATCH/DELETE /nodes/{node_id} - contracts/rest/openapi.yaml.

This is the in-scope CRUD surface for this slice (TZ-5 platform/README.md scope
item 3). Cascade-reconfiguration-on-decommission and actual Agent command dispatch
are explicit TODOs, called out inline below and in platform/README.md.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dist_vpn_platform.api.deps import require_admin
from dist_vpn_platform.config import settings
from dist_vpn_platform.db import get_db
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.models.cascade import CascadeExitNode
from dist_vpn_platform.models.enrollment_token import EnrollmentToken
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.schemas.nodes import (
    NodeEnrollRequest,
    NodeEnrollResponse,
    NodeListResponse,
    NodeOut,
    NodePatchRequest,
)
from dist_vpn_platform.security.enrollment_tokens import generate_token, hash_token

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("dist_vpn_platform.audit")

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.get("", response_model=NodeListResponse)
async def list_nodes(
    role: NodeRole | None = Query(default=None),
    status_: NodeStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> NodeListResponse:
    stmt = select(Node)
    count_stmt = select(func.count()).select_from(Node)
    if role is not None:
        stmt = stmt.where(Node.role == role)
        count_stmt = count_stmt.where(Node.role == role)
    if status_ is not None:
        stmt = stmt.where(Node.status == status_)
        count_stmt = count_stmt.where(Node.status == status_)

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(Node.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(stmt)).scalars().all()

    return NodeListResponse(items=[NodeOut.model_validate(n) for n in items], total=total)


@router.post("", response_model=NodeEnrollResponse, status_code=status.HTTP_201_CREATED)
async def create_enrollment_token(
    body: NodeEnrollRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> NodeEnrollResponse:
    plaintext = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.enrollment_token_ttl_minutes)

    token_row = EnrollmentToken(
        token_hash=hash_token(plaintext),
        requested_role=body.requested_role,
        label=body.label,
        created_by_admin_id=admin.id,
        expires_at=expires_at,
    )
    db.add(token_row)
    await db.commit()

    audit_logger.info(
        "enrollment_token_created",
        extra={"admin_id": admin.id, "requested_role": body.requested_role.value},
    )
    # The plaintext token is returned exactly once and never persisted (TZ-5 §6.2).
    return NodeEnrollResponse(enrollment_token=plaintext, expires_at=expires_at)


@router.get("/{node_id}", response_model=NodeOut)
async def get_node(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> NodeOut:
    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    return NodeOut.model_validate(node)


@router.patch("/{node_id}", response_model=NodeOut)
async def patch_node(
    node_id: str,
    body: NodePatchRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> NodeOut:
    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")

    if body.role is not None:
        node.role = body.role
    if body.label is not None:
        node.label = body.label

    await db.commit()
    await db.refresh(node)

    audit_logger.info("node_patched", extra={"admin_id": admin.id, "node_id": node_id})
    return NodeOut.model_validate(node)


@router.delete("/{node_id}", status_code=status.HTTP_202_ACCEPTED)
async def decommission_node(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> dict:
    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")

    previous_status = node.status
    node.status = NodeStatus.DECOMMISSIONED
    node.decommissioned_at = datetime.now(timezone.utc)

    # Remove this node from any cascade's exit-candidate pool so no "dangling
    # reference" survives the decommission (TZ-5 §4.1). Entry-side reconfiguration
    # (rebuilding/retiring the cascade itself when its *entry* node is decommissioned)
    # and the actual Agent-side RemoveOutbound/RemoveInbound command dispatch are
    # TODO for the cascade-assembly follow-up slice (see platform/README.md) - this
    # slice only guarantees the Platform-side registry no longer references the node.
    await db.execute(delete(CascadeExitNode).where(CascadeExitNode.exit_node_id == node_id))

    db.add(
        HealthEvent(
            node_id=node_id,
            from_status=previous_status.value,
            to_status=NodeStatus.DECOMMISSIONED.value,
            reason=f"Decommissioned by admin {admin.username}",
        )
    )
    await db.commit()

    audit_logger.info("node_decommissioned", extra={"admin_id": admin.id, "node_id": node_id})
    return {"detail": "accepted", "node_id": node_id}
