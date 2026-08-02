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

POST /cascades/{id}/test and GET /cascades/{id}/tests (TZ-5 §4.3) are now real too -
see services/link_tests.py for the RunTest dispatch/LinkTest persistence and
api/routes/tests.py for GET /tests/{id}.

PATCH /cascades/{id} and GET /cascades/{id}/events remain out of scope for this slice
(not requested) - still declared with 501 below so the contract shape stays visible.
"""
from __future__ import annotations

import logging
from datetime import datetime

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
from dist_vpn_platform.models.cascade import Cascade, CascadeExitLink, CascadeExitLinkStatus, CascadeStatus
from dist_vpn_platform.models.exit_link_allocation import ExitLinkAllocation, ExitLinkAllocationStatus
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.link_test import LinkTest
from dist_vpn_platform.models.node import Node
from dist_vpn_platform.models.remote_exit_candidate import RemoteExitCandidate, RemoteExitCandidateStatus
from dist_vpn_platform.models.user import User, UserCascade
from dist_vpn_platform.schemas.cascades import (
    CascadeExitLinkOut,
    CascadeInput,
    CascadeListResponse,
    CascadeMigrateUsersRequest,
    CascadeMigrateUsersResponse,
    CascadeOut,
)
from dist_vpn_platform.schemas.health_events import HealthEventOut
from dist_vpn_platform.schemas.tests import LinkTestListResponse, LinkTestOut, LinkTestStartResponse
from dist_vpn_platform.security import reality_keys
from dist_vpn_platform.services import link_tests
from dist_vpn_platform.services import xray_templates as xray
from dist_vpn_platform.services.event_bus import publish_cascade_event, publish_health_event

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("dist_vpn_platform.audit")

router = APIRouter(prefix="/cascades", tags=["cascades"])

_NOT_IMPLEMENTED = "Not implemented in this slice (TZ-5 §4.2/§4.3 - follow-up slice)."


async def _get_or_create_local_remote_exit_candidate(db: AsyncSession, exit_node: Node) -> RemoteExitCandidate:
    """Local/degenerate path (Platform not actually split into RU-segment/foreign-
    segment instances yet, or this exit node happens to be co-located in the same DB
    as the entry side) - see models/remote_exit_candidate.py's docstring for the
    peer_platform_id=NULL convention this relies on. A real-peer branch (call
    services/peering_client.py::provision_exit_link instead of creating this row
    directly) is not wired up yet - no request shape this route accepts today can
    reach it, since CascadeInput.exit_node_ids are always local Node ids for now."""
    stmt = select(RemoteExitCandidate).where(
        RemoteExitCandidate.peer_platform_id.is_(None),
        RemoteExitCandidate.remote_node_id == exit_node.id,
    )
    candidate = (await db.execute(stmt)).scalar_one_or_none()
    if candidate is None:
        candidate = RemoteExitCandidate(
            peer_platform_id=None,
            remote_node_id=exit_node.id,
            label=exit_node.label,
            endpoint_address=exit_node.public_address or exit_node.hostname,
            status=RemoteExitCandidateStatus.HEALTHY
            if exit_node.status.value == "healthy"
            else RemoteExitCandidateStatus.UNKNOWN,
        )
        db.add(candidate)
        await db.flush()
    return candidate


def _cascade_exit_link_out(link: CascadeExitLink) -> CascadeExitLinkOut:
    return CascadeExitLinkOut(
        remote_exit_candidate_id=link.remote_exit_candidate_id,
        remote_node_id=link.remote_exit_candidate.remote_node_id,
        status=link.status,
        active_preset=link.active_preset,
    )


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
        .options(selectinload(Cascade.exit_links).selectinload(CascadeExitLink.remote_exit_candidate))
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
                exit_links=[_cascade_exit_link_out(link) for link in c.exit_links],
                status=c.status,
                hop_a_presets=c.hop_a_presets,
                hopa_reality_public_key=c.hopa_reality_public_key,
                hopa_reality_short_id=c.hopa_reality_short_id,
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

    hopa_private_key, hopa_public_key = reality_keys.generate_reality_keypair()
    cascade = Cascade(
        entry_node_id=entry_node.id,
        hop_a_presets=list(body.hop_a_presets),
        status=CascadeStatus.DOWN,
        hopa_reality_private_key=hopa_private_key,
        hopa_reality_public_key=hopa_public_key,
        hopa_reality_short_id=reality_keys.generate_short_id(),
    )
    db.add(cascade)
    await db.flush()  # assign cascade.id before it's referenced below

    # hopb_reality_private_key now lives on ExitLinkAllocation (the side that will
    # actually dispatch the matching AddInbound to its own exit-node Agent), not on
    # CascadeExitLink - see models/exit_link_allocation.py's docstring. In this
    # local/degenerate path (no real peer Platform yet) that's still this same
    # request/process, so both rows are created together here.
    exit_links: dict[str, CascadeExitLink] = {}
    exit_allocations: dict[str, ExitLinkAllocation] = {}
    for exit_node in exit_nodes:
        candidate = await _get_or_create_local_remote_exit_candidate(db, exit_node)

        hopb_private_key, hopb_public_key = reality_keys.generate_reality_keypair()
        hopb_short_id = reality_keys.generate_short_id()

        allocation = ExitLinkAllocation(
            peer_platform_id=None,
            exit_node_id=exit_node.id,
            remote_cascade_id=cascade.id,
            hopb_reality_private_key=hopb_private_key,
            hopb_reality_public_key=hopb_public_key,
            hopb_reality_short_id=hopb_short_id,
            status=ExitLinkAllocationStatus.PENDING,
        )
        db.add(allocation)
        await db.flush()  # assign allocation.id before it's referenced as remote_link_id below

        link = CascadeExitLink(
            cascade_id=cascade.id,
            remote_exit_candidate_id=candidate.id,
            remote_link_id=allocation.id,
            status=CascadeExitLinkStatus.PENDING,
            hopb_reality_public_key=hopb_public_key,
            hopb_reality_short_id=hopb_short_id,
        )
        db.add(link)
        link.remote_exit_candidate = candidate  # avoid a re-SELECT for _cascade_exit_link_out below
        exit_links[exit_node.id] = link
        exit_allocations[exit_node.id] = allocation
    await db.flush()

    non_p4_presets = [p for p in body.hop_a_presets if p != "p4"]
    hop_a_preset = non_p4_presets[0] if non_p4_presets else "p1"

    entry_ok = await dispatch_best_effort(
        entry_node.id,
        pb.Command(
            command_id=new_command_id(),
            add_inbound=pb.AddInbound(
                inbound_json=xray.build_hopa_inbound_json(
                    cascade.id, hop_a_preset, cascade.hopa_reality_private_key, cascade.hopa_reality_short_id
                )
            ),
        ),
        context=f"cascade {cascade.id} hopa inbound",
    )

    # P4 (VLESS+TLS/WebSocket, TZ-4 §3.3) is a second, simultaneous inbound alongside
    # the REALITY one above, not a replacement - see xray_templates.py's P4 constants.
    # Best-effort and does NOT affect cascade.status below: REALITY is the required
    # path, P4 is an optional fallback a client only needs if REALITY is unreachable.
    if "p4" in body.hop_a_presets:
        p4_ok = await dispatch_best_effort(
            entry_node.id,
            pb.Command(
                command_id=new_command_id(),
                add_inbound=pb.AddInbound(inbound_json=xray.build_hopa_p4_inbound_json(cascade.id)),
            ),
            context=f"cascade {cascade.id} hopa p4 inbound",
        )
        if not p4_ok:
            logger.warning("cascade_p4_inbound_dispatch_failed", extra={"cascade_id": cascade.id})

    exit_ok_count = 0
    for exit_node in exit_nodes:
        link = exit_links[exit_node.id]
        allocation = exit_allocations[exit_node.id]
        outbound_ok = await dispatch_best_effort(
            entry_node.id,
            pb.Command(
                command_id=new_command_id(),
                add_outbound=pb.AddOutbound(
                    outbound_json=xray.build_hopb_outbound_json(
                        cascade.id,
                        exit_node.id,
                        exit_node.public_address or exit_node.hostname or exit_node.id,
                        link.hopb_reality_public_key,
                        link.hopb_reality_short_id,
                    )
                ),
            ),
            context=f"cascade {cascade.id} hopb outbound -> {exit_node.id}",
        )
        entry_ok = entry_ok and outbound_ok

        # Private key: allocation.hopb_reality_private_key, not link.* - see
        # models/exit_link_allocation.py's docstring (this Platform is the one
        # dispatching the matching AddInbound, so it's also the one that generated
        # and holds the private half; CascadeExitLink only ever gets the public half).
        inbound_ok = await dispatch_best_effort(
            exit_node.id,
            pb.Command(
                command_id=new_command_id(),
                add_inbound=pb.AddInbound(
                    inbound_json=xray.build_hopb_inbound_json(
                        cascade.id, exit_node.id, allocation.hopb_reality_private_key, allocation.hopb_reality_short_id
                    )
                ),
            ),
            context=f"cascade {cascade.id} hopb inbound on {exit_node.id}",
        )
        link.status = CascadeExitLinkStatus.UP if inbound_ok else CascadeExitLinkStatus.DOWN
        allocation.status = ExitLinkAllocationStatus.UP if inbound_ok else ExitLinkAllocationStatus.DOWN
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

    health_event = HealthEvent(
        cascade_id=cascade.id,
        from_status=CascadeStatus.DOWN.value,
        to_status=cascade.status.value,
        reason=f"cascade created by admin {admin.username}",
    )
    db.add(health_event)
    await db.commit()

    cascade_out = CascadeOut(
        id=cascade.id,
        entry_node_id=cascade.entry_node_id,
        exit_node_ids=[n.id for n in exit_nodes],
        exit_links=[_cascade_exit_link_out(exit_links[n.id]) for n in exit_nodes],
        status=cascade.status,
        hop_a_presets=cascade.hop_a_presets,
        hopa_reality_public_key=cascade.hopa_reality_public_key,
        hopa_reality_short_id=cascade.hopa_reality_short_id,
    )

    publish_health_event(health_event)  # TZ-5 §4.3: push to WS /events subscribers
    # Also the full Cascade snapshot, not just the HealthEvent row - see
    # event_bus.py::publish_cascade_event's docstring for why the health_event alone
    # was never enough for Admin Client to render a brand-new cascade live.
    publish_cascade_event(cascade_out)
    audit_logger.info(
        "cascade_created",
        extra={"admin_id": admin.id, "cascade_id": cascade.id, "status": cascade.status.value},
    )

    return cascade_out


@router.get("/{cascade_id}", response_model=CascadeOut)
async def get_cascade(
    cascade_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> CascadeOut:
    stmt = (
        select(Cascade)
        .options(selectinload(Cascade.exit_links).selectinload(CascadeExitLink.remote_exit_candidate))
        .where(Cascade.id == cascade_id)
    )
    cascade = (await db.execute(stmt)).scalar_one_or_none()
    if cascade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cascade not found")

    return CascadeOut(
        id=cascade.id,
        entry_node_id=cascade.entry_node_id,
        exit_node_ids=cascade.exit_node_ids,
        exit_links=[_cascade_exit_link_out(link) for link in cascade.exit_links],
        status=cascade.status,
        hop_a_presets=cascade.hop_a_presets,
        hopa_reality_public_key=cascade.hopa_reality_public_key,
        hopa_reality_short_id=cascade.hopa_reality_short_id,
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
    stmt = (
        select(Cascade)
        .options(selectinload(Cascade.exit_links).selectinload(CascadeExitLink.remote_exit_candidate))
        .where(Cascade.id == cascade_id)
    )
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
    # by default" pattern as api/routes/nodes.py's decommission_node(). CascadeExitLink
    # rows, by contrast, DO have `cascade="all, delete-orphan"` on Cascade.exit_links
    # (models/cascade.py) and are already loaded via selectinload above, so db.delete()
    # handles them - an extra raw DELETE here would just race the ORM's own cascade
    # delete for the same rows.
    #
    # ExitLinkAllocation has no ORM relationship to Cascade at all (it's keyed by the
    # opaque remote_cascade_id, not a real FK - see models/exit_link_allocation.py's
    # docstring), so it needs the same explicit cleanup UserCascade does. Only the
    # local/degenerate rows this Platform created for this cascade are ever deletable
    # here (peer_platform_id IS NULL) - a real peer's own ExitLinkAllocation rows live
    # in that peer's database and are its own responsibility to clean up (slice 10+
    # will add a ReleaseExitLink peering call for that case).
    await db.execute(
        delete(ExitLinkAllocation).where(
            ExitLinkAllocation.peer_platform_id.is_(None),
            ExitLinkAllocation.remote_cascade_id == cascade_id,
        )
    )
    await db.execute(delete(UserCascade).where(UserCascade.cascade_id == cascade_id))
    await db.delete(cascade)
    await db.commit()

    audit_logger.info("cascade_deleted", extra={"admin_id": admin.id, "cascade_id": cascade_id})


@router.post("/{cascade_id}/migrate-users", response_model=CascadeMigrateUsersResponse)
async def migrate_cascade_users(
    cascade_id: str,
    body: CascadeMigrateUsersRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> CascadeMigrateUsersResponse:
    """The admin-triggered half of failover (TZ-5 §4.3 "перенаправлять подключения
    при обрыве на доступные сервера"): services/health_scheduler.py auto-detects a
    DOWN cascade (entry node heartbeat timeout) and marks it as such, but *choosing*
    which other cascade should take over is a judgment call this slice deliberately
    leaves to the admin, not automated - a scheduler picking a "healthy-looking"
    replacement on its own risks mass-migrating users onto a cascade the admin
    would not have chosen (wrong region, wrong provider, mid-maintenance, ...).

    Moves every user currently on `cascade_id` onto `to_cascade_id`: RemoveUser on
    the old cascade's entry node (best-effort courtesy cleanup - if that node is
    genuinely down, as the typical failover scenario implies, this simply won't
    land, which is fine, nothing depends on it succeeding) + AddUser on the new
    cascade's entry node (the one dispatch that actually matters), then updates the
    UserCascade rows to match. Does NOT require `cascade_id` to actually be DOWN -
    an admin might migrate for planned maintenance too - and does NOT delete or
    otherwise touch the source cascade itself, only its User memberships.
    """
    if body.to_cascade_id == cascade_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "to_cascade_id must differ from cascade_id")

    from_cascade = await db.get(Cascade, cascade_id)
    if from_cascade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"cascade not found: {cascade_id}")
    to_cascade = await db.get(Cascade, body.to_cascade_id)
    if to_cascade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"target cascade not found: {body.to_cascade_id}")

    stmt = (
        select(UserCascade)
        .where(UserCascade.cascade_id == cascade_id)
        .options(selectinload(UserCascade.user))
    )
    links = (await db.execute(stmt)).scalars().all()

    migrated = 0
    for link in links:
        user: User = link.user

        await dispatch_best_effort(
            from_cascade.entry_node_id,
            pb.Command(
                command_id=new_command_id(),
                remove_user=pb.RemoveUser(inbound_tag=xray.hopa_inbound_tag(cascade_id), email=user.uuid),
            ),
            context=f"migrate user {user.id} off cascade {cascade_id}",
        )
        add_ok = await dispatch_best_effort(
            to_cascade.entry_node_id,
            pb.Command(
                command_id=new_command_id(),
                add_user=pb.AddUser(
                    inbound_tag=xray.hopa_inbound_tag(to_cascade.id), uuid=user.uuid, email=user.uuid, flow=""
                ),
            ),
            context=f"migrate user {user.id} onto cascade {to_cascade.id}",
        )
        if not add_ok:
            logger.warning(
                "cascade_migrate_add_user_failed",
                extra={"user_id": user.id, "from_cascade_id": cascade_id, "to_cascade_id": to_cascade.id},
            )

        await db.delete(link)
        # A user might already have standing access to the target cascade (e.g. was
        # deliberately granted both as a pre-existing backup) - UserCascade's
        # composite primary key (user_id, cascade_id) would reject a duplicate row.
        existing_target_link = await db.get(UserCascade, (user.id, to_cascade.id))
        if existing_target_link is None:
            db.add(UserCascade(user_id=user.id, cascade_id=to_cascade.id))
        migrated += 1

    await db.commit()

    audit_logger.info(
        "cascade_users_migrated",
        extra={
            "admin_id": admin.id,
            "cascade_id": cascade_id,
            "to_cascade_id": to_cascade.id,
            "migrated_user_count": migrated,
        },
    )
    return CascadeMigrateUsersResponse(
        cascade_id=cascade_id, to_cascade_id=to_cascade.id, migrated_user_count=migrated
    )


@router.get("/{cascade_id}/events", response_model=list[HealthEventOut])
async def list_cascade_events(
    cascade_id: str,
    since: datetime | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> list[HealthEventOut]:
    """Journal of this cascade's status transitions (TZ-5 §4.3 "что и где отвалилось")
    - HealthEvent rows already exist (written on create/delete/migrate-users/
    health_scheduler timeout, published to WS /events as they happen) - this endpoint
    is the pull/history counterpart to that push. Bare array + `since` filter, per
    the contract's own original shape (contracts/rest/openapi.yaml) - unlike several
    other additive endpoints in this file, this path's shape was specified from the
    start, not something this slice is free to invent an items+total envelope for."""
    cascade = await db.get(Cascade, cascade_id)
    if cascade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cascade not found")

    stmt = select(HealthEvent).where(HealthEvent.cascade_id == cascade_id).order_by(HealthEvent.occurred_at.desc())
    if since is not None:
        stmt = stmt.where(HealthEvent.occurred_at >= since)
    items = (await db.execute(stmt)).scalars().all()

    return [HealthEventOut.model_validate(item) for item in items]


@router.post("/{cascade_id}/test", status_code=status.HTTP_202_ACCEPTED, response_model=LinkTestStartResponse)
async def start_cascade_test(
    cascade_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminAccount = Depends(require_admin),
) -> LinkTestStartResponse:
    cascade = await db.get(Cascade, cascade_id)
    if cascade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cascade not found")

    # services/link_tests.py persists the LinkTest row and dispatches RunTest to the
    # entry node itself (own session_scope(), independent of this request's `db`) -
    # returns as soon as the row exists, never blocks on the Agent (TZ-5 §4.3: "202,
    # результат — через /tests/{test_id} или WebSocket").
    link_test = await link_tests.start_link_test(cascade_id, cascade.entry_node_id, initiated_by=admin.username)

    audit_logger.info(
        "cascade_test_started",
        extra={"admin_id": admin.id, "cascade_id": cascade_id, "test_id": link_test.id},
    )
    return LinkTestStartResponse(test_id=link_test.id)


@router.get("/{cascade_id}/tests", response_model=LinkTestListResponse)
async def list_cascade_tests(
    cascade_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _admin: AdminAccount = Depends(require_admin),
) -> LinkTestListResponse:
    cascade = await db.get(Cascade, cascade_id)
    if cascade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cascade not found")

    total = (
        await db.execute(select(func.count()).select_from(LinkTest).where(LinkTest.cascade_id == cascade_id))
    ).scalar_one()
    stmt = (
        select(LinkTest)
        .where(LinkTest.cascade_id == cascade_id)
        .order_by(LinkTest.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()

    return LinkTestListResponse(items=[LinkTestOut.model_validate(item) for item in items], total=total)
