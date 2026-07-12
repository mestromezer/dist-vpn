"""GET/POST/DELETE /cascades, GET /cascades/{id} - contracts/rest/openapi.yaml.

Cascade assembly (TZ-5 §4.2) is now implemented: POST validates the entry/exit nodes,
persists Cascade+CascadeExitNode rows, and dispatches the corresponding
AddInbound/AddOutbound/AddRoutingRule commands (services/xray_templates.py) to the
relevant Agents over the live command-dispatch mechanism (grpc_server/servicer.py).
DELETE dispatches the matching Remove* commands and tears down the persisted rows.

Dispatch is best-effort (api/command_dispatch.py) - an Agent that's temporarily
disconnected does not fail the HTTP request; instead Cascade.status reflects what
actually got wired up (UP/DEGRADED/DOWN), same non-blocking-on-Agent-availability
philosophy already used for node decommissioning (api/routes/nodes.py). Actual
automatic reconciliation once a lagging Agent reconnects is not built in this slice -
see platform/README.md TODO.

PATCH /cascades/{id}, GET /cascades/{id}/events and POST /cascades/{id}/test remain
out of scope for this slice (not requested) - still declared with 501 below so the
contract shape stays visible.
"""
from __future__ import annotations

import logging

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
from dist_vpn_platform.models.cascade import Cascade, CascadeExitNode, CascadeStatus
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node
from dist_vpn_platform.models.user import UserCascade
from dist_vpn_platform.schemas.cascades import CascadeInput, CascadeListResponse, CascadeOut
from dist_vpn_platform.services import xray_templates as xray

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("dist_vpn_platform.audit")

router = APIRouter(prefix="/cascades", tags=["cascades"])

_NOT_IMPLEMENTED = "Not implemented in this slice (TZ-5 §4.2/§4.3 - follow-up slice)."


@router.get("", response_model=CascadeListResponse)
async def list_cascades(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> CascadeListResponse:
    total = (await db.execute(select(func.count()).select_from(Cascade))).scalar_one()

    stmt = (
        select(Cascade)
        .options(selectinload(Cascade.exit_links))
        .order_by(Cascade.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()

    return CascadeListResponse(
        items=[
            CascadeOut(
                id=c.id,
                entry_node_id=c.entry_node_id,
                exit_node_ids=c.exit_node_ids,
                status=c.status,
                hop_a_presets=c.hop_a_presets,
            )
            for c in items
        ],
        total=total,
    )


@router.post("", response_model=CascadeOut, status_code=status.HTTP_201_CREATED)
async def create_cascade(
    body: CascadeInput,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> CascadeOut:
    entry_node = await db.get(Node, body.entry_node_id)
    if entry_node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"entry node not found: {body.entry_node_id}")

    exit_nodes: list[Node] = []
    for exit_node_id in body.exit_node_ids:
        exit_node = await db.get(Node, exit_node_id)
        if exit_node is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"exit node not found: {exit_node_id}")
        exit_nodes.append(exit_node)

    cascade = Cascade(
        entry_node_id=entry_node.id,
        hop_a_presets=list(body.hop_a_presets),
        status=CascadeStatus.DOWN,
    )
    db.add(cascade)
    await db.flush()  # assign cascade.id before it's referenced below

    for exit_node in exit_nodes:
        db.add(CascadeExitNode(cascade_id=cascade.id, exit_node_id=exit_node.id))
    await db.flush()

    hop_a_preset = body.hop_a_presets[0] if body.hop_a_presets else "p1"

    entry_ok = await dispatch_best_effort(
        entry_node.id,
        pb.Command(
            command_id=new_command_id(),
            add_inbound=pb.AddInbound(inbound_json=xray.build_hopa_inbound_json(cascade.id, hop_a_preset)),
        ),
        context=f"cascade {cascade.id} hopa inbound",
    )

    exit_ok_count = 0
    for exit_node in exit_nodes:
        outbound_ok = await dispatch_best_effort(
            entry_node.id,
            pb.Command(
                command_id=new_command_id(),
                add_outbound=pb.AddOutbound(
                    outbound_json=xray.build_hopb_outbound_json(
                        cascade.id, exit_node.id, exit_node.hostname or exit_node.id
                    )
                ),
            ),
            context=f"cascade {cascade.id} hopb outbound -> {exit_node.id}",
        )
        entry_ok = entry_ok and outbound_ok

        inbound_ok = await dispatch_best_effort(
            exit_node.id,
            pb.Command(
                command_id=new_command_id(),
                add_inbound=pb.AddInbound(inbound_json=xray.build_hopb_inbound_json(cascade.id)),
            ),
            context=f"cascade {cascade.id} hopb inbound on {exit_node.id}",
        )
        if inbound_ok:
            exit_ok_count += 1

    if exit_nodes:
        routing_ok = await dispatch_best_effort(
            entry_node.id,
            pb.Command(
                command_id=new_command_id(),
                add_routing_rule=pb.AddRoutingRule(
                    rule_tag=xray.routing_rule_tag(cascade.id),
                    rule_json=xray.build_balancer_routing_rule_json(cascade.id),
                ),
            ),
            context=f"cascade {cascade.id} routing rule",
        )
        entry_ok = entry_ok and routing_ok

    if not entry_ok:
        cascade.status = CascadeStatus.DOWN
    elif exit_ok_count < len(exit_nodes):
        cascade.status = CascadeStatus.DEGRADED
    else:
        cascade.status = CascadeStatus.UP

    db.add(
        HealthEvent(
            cascade_id=cascade.id,
            from_status=CascadeStatus.DOWN.value,
            to_status=cascade.status.value,
            reason=f"cascade created by admin {admin.username}",
        )
    )
    await db.commit()

    audit_logger.info(
        "cascade_created",
        extra={"admin_id": admin.id, "cascade_id": cascade.id, "status": cascade.status.value},
    )

    return CascadeOut(
        id=cascade.id,
        entry_node_id=cascade.entry_node_id,
        exit_node_ids=[n.id for n in exit_nodes],
        status=cascade.status,
        hop_a_presets=cascade.hop_a_presets,
    )


@router.get("/{cascade_id}", response_model=CascadeOut)
async def get_cascade(
    cascade_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> CascadeOut:
    stmt = select(Cascade).options(selectinload(Cascade.exit_links)).where(Cascade.id == cascade_id)
    cascade = (await db.execute(stmt)).scalar_one_or_none()
    if cascade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cascade not found")

    return CascadeOut(
        id=cascade.id,
        entry_node_id=cascade.entry_node_id,
        exit_node_ids=cascade.exit_node_ids,
        status=cascade.status,
        hop_a_presets=cascade.hop_a_presets,
    )


@router.patch("/{cascade_id}")
async def patch_cascade(
    cascade_id: str, body: CascadeInput, _admin: AdminAccount = Depends(require_admin)
) -> None:
    # TODO(follow-up slice): re-diff the exit-node pool and dispatch the incremental
    # AddOutbound/RemoveOutbound set rather than the full create/delete this slice
    # implements - not requested for this slice, see platform/README.md TODO.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.delete("/{cascade_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cascade(
    cascade_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> None:
    stmt = select(Cascade).options(selectinload(Cascade.exit_links)).where(Cascade.id == cascade_id)
    cascade = (await db.execute(stmt)).scalar_one_or_none()
    if cascade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cascade not found")

    exit_node_ids = cascade.exit_node_ids
    entry_node_id = cascade.entry_node_id

    await dispatch_best_effort(
        entry_node_id,
        pb.Command(command_id=new_command_id(), remove_inbound=pb.RemoveInbound(tag=xray.hopa_inbound_tag(cascade_id))),
        context=f"cascade {cascade_id} remove hopa inbound",
    )
    if exit_node_ids:
        await dispatch_best_effort(
            entry_node_id,
            pb.Command(
                command_id=new_command_id(),
                remove_routing_rule=pb.RemoveRoutingRule(rule_tag=xray.routing_rule_tag(cascade_id)),
            ),
            context=f"cascade {cascade_id} remove routing rule",
        )
    for exit_node_id in exit_node_ids:
        await dispatch_best_effort(
            entry_node_id,
            pb.Command(
                command_id=new_command_id(),
                remove_outbound=pb.RemoveOutbound(tag=xray.hopb_outbound_tag(cascade_id, exit_node_id)),
            ),
            context=f"cascade {cascade_id} remove hopb outbound -> {exit_node_id}",
        )
        await dispatch_best_effort(
            exit_node_id,
            pb.Command(
                command_id=new_command_id(),
                remove_inbound=pb.RemoveInbound(tag=xray.hopb_inbound_tag(cascade_id)),
            ),
            context=f"cascade {cascade_id} remove hopb inbound on {exit_node_id}",
        )

    # UserCascade has no ORM relationship on the Cascade side (only User ->
    # UserCascade, see models/user.py), so it needs an explicit cleanup - same
    # "don't rely on DB-level ON DELETE CASCADE, SQLite in tests doesn't enforce FKs
    # by default" pattern as api/routes/nodes.py's decommission_node(). CascadeExitNode
    # rows, by contrast, DO have `cascade="all, delete-orphan"` on Cascade.exit_links
    # (models/cascade.py) and are already loaded via selectinload above, so db.delete()
    # handles them - an extra raw DELETE here would just race the ORM's own cascade
    # delete for the same rows.
    await db.execute(delete(UserCascade).where(UserCascade.cascade_id == cascade_id))
    await db.delete(cascade)
    await db.commit()

    audit_logger.info("cascade_deleted", extra={"admin_id": admin.id, "cascade_id": cascade_id})


@router.get("/{cascade_id}/events")
async def list_cascade_events(cascade_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO: cheap to implement against the existing HealthEvent table (rows are
    # already written on cascade create/delete above) - not requested for this slice.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)


@router.post("/{cascade_id}/test", status_code=status.HTTP_202_ACCEPTED)
async def start_cascade_test(cascade_id: str, _admin: AdminAccount = Depends(require_admin)) -> None:
    # TODO(follow-up slice): dispatch_command(entry_node_id, Command(run_test=...)),
    # correlate the async TestResult by test_id (TZ-5 §4.3) - the dispatch mechanism
    # this needs (grpc_server/servicer.py resolve()-by-test_id) already exists as of
    # this slice, this route just isn't wired to it yet (not requested).
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_IMPLEMENTED)
