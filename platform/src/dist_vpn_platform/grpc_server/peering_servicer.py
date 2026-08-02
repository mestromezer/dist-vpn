"""PlatformPeeringServicer - the exit-segment side's implementation of
contracts/proto/platform_peering.proto. Only ever instantiated/served when
settings.platform_segment == "exit" (see grpc_server/peering_server.py, main.py) -
RU-Platform is always the client (services/peering_client.py) and never runs this
servicer, per the protocol's fixed dial direction (platform_peering.proto's header
comment).

Every RPC operates on ExitLinkAllocation rows (models/exit_link_allocation.py) scoped
to `self.peer_platform_id` - the single PeerPlatform row this server's TLS credentials
were pinned to at startup (grpc_server/peering_server.py resolves it once, before
building grpc.ssl_server_credentials, since the pinned peer_cert_pem IS the server's
only trust root - a connection could not have reached this servicer at all unless it
already authenticated as that specific peer).

Preset-aware dispatch (xray_templates.build_hopb_inbound_json gaining a `preset`
parameter) lands in slice 11 - this slice calls the current P1-only signature, so
SwitchLinkPreset updates the stored active_preset/regenerates REALITY keys and
re-dispatches, but does not yet change the actual wire transport. Real per-preset
rotation becomes meaningful once slice 11's builders exist; this slice is the
plumbing, not that yet.
"""
from __future__ import annotations

import logging

import grpc
from sqlalchemy import select

from dist_vpn_platform.api.command_dispatch import dispatch_best_effort
from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import agent_control_pb2 as agent_pb
from dist_vpn_platform.generated import platform_peering_pb2 as pb
from dist_vpn_platform.generated import platform_peering_pb2_grpc as pb_grpc
from dist_vpn_platform.grpc_server.servicer import new_command_id
from dist_vpn_platform.models.exit_link_allocation import ExitLinkAllocation, ExitLinkAllocationStatus
from dist_vpn_platform.models.node import Node, NodeStatus
from dist_vpn_platform.security import reality_keys
from dist_vpn_platform.services import xray_templates as xray

logger = logging.getLogger(__name__)

_STATUS_TO_PROTO = {
    ExitLinkAllocationStatus.PENDING: pb.LINK_HEALTH_STATUS_UNSPECIFIED,
    ExitLinkAllocationStatus.UP: pb.LINK_HEALTH_STATUS_UP,
    ExitLinkAllocationStatus.DEGRADED: pb.LINK_HEALTH_STATUS_DEGRADED,
    ExitLinkAllocationStatus.DOWN: pb.LINK_HEALTH_STATUS_DOWN,
}


class PlatformPeeringServicer(pb_grpc.PlatformPeeringServicer):
    def __init__(self, *, peer_platform_id: str) -> None:
        self._peer_platform_id = peer_platform_id

    async def ProvisionExitLink(
        self, request: pb.ProvisionExitLinkRequest, context: grpc.aio.ServicerContext
    ) -> pb.ProvisionExitLinkResponse:
        async with session_scope() as db:
            exit_node = await db.get(Node, request.exit_node_id)
            if exit_node is None:
                return pb.ProvisionExitLinkResponse(accepted=False, reason="unknown exit_node_id")
            # Only DECOMMISSIONED is rejected up front - matches api/routes/
            # cascades.py::create_cascade's own philosophy of not gating on the
            # (lagging, DB-cached) Node.status field otherwise: a live
            # SessionRegistry connection (checked by dispatch_best_effort below, not
            # here) is the real signal, and ENROLLING/DOWN nodes can still validly
            # receive a dispatch attempt whose result determines the allocation's
            # own status (see test_create_cascade_with_offline_exit_node_is_degraded
            # for the precedent this mirrors).
            if exit_node.status == NodeStatus.DECOMMISSIONED:
                return pb.ProvisionExitLinkResponse(
                    accepted=False, reason=f"exit node is not usable (status={exit_node.status.value})"
                )

            existing = (
                await db.execute(
                    select(ExitLinkAllocation).where(
                        ExitLinkAllocation.peer_platform_id == self._peer_platform_id,
                        ExitLinkAllocation.remote_cascade_id == request.remote_cascade_id,
                        ExitLinkAllocation.exit_node_id == request.exit_node_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                # Idempotent re-provision (e.g. a retried request after a dropped
                # response) - do not regenerate keys or re-dispatch, just echo back
                # what already exists.
                return pb.ProvisionExitLinkResponse(
                    accepted=True,
                    link_id=existing.id,
                    hopb_reality_public_key=existing.hopb_reality_public_key or "",
                    hopb_reality_short_id=existing.hopb_reality_short_id or "",
                    endpoint_address=exit_node.public_address or exit_node.hostname or exit_node.id,
                )

            preset = request.initial_preset or "p1"
            private_key, public_key = reality_keys.generate_reality_keypair()
            short_id = reality_keys.generate_short_id()

            allocation = ExitLinkAllocation(
                peer_platform_id=self._peer_platform_id,
                exit_node_id=exit_node.id,
                remote_cascade_id=request.remote_cascade_id,
                hopb_reality_private_key=private_key,
                hopb_reality_public_key=public_key,
                hopb_reality_short_id=short_id,
                active_preset=preset,
                status=ExitLinkAllocationStatus.PENDING,
            )
            db.add(allocation)
            await db.flush()

            inbound_ok = await dispatch_best_effort(
                exit_node.id,
                agent_pb.Command(
                    command_id=new_command_id(),
                    add_inbound=agent_pb.AddInbound(
                        inbound_json=xray.build_hopb_inbound_json(
                            request.remote_cascade_id, exit_node.id, private_key, short_id
                        )
                    ),
                ),
                context=f"peering provision-exit-link {allocation.id} on {exit_node.id}",
            )
            allocation.status = ExitLinkAllocationStatus.UP if inbound_ok else ExitLinkAllocationStatus.DOWN

            return pb.ProvisionExitLinkResponse(
                accepted=True,
                link_id=allocation.id,
                hopb_reality_public_key=public_key,
                hopb_reality_short_id=short_id,
                endpoint_address=exit_node.public_address or exit_node.hostname or exit_node.id,
            )

    async def ReleaseExitLink(
        self, request: pb.ReleaseExitLinkRequest, context: grpc.aio.ServicerContext
    ) -> pb.ReleaseExitLinkResponse:
        async with session_scope() as db:
            allocation = await db.get(ExitLinkAllocation, request.link_id)
            if allocation is None or allocation.peer_platform_id != self._peer_platform_id:
                return pb.ReleaseExitLinkResponse(released=False)  # idempotent, not an error

            await dispatch_best_effort(
                allocation.exit_node_id,
                agent_pb.Command(
                    command_id=new_command_id(),
                    remove_inbound=agent_pb.RemoveInbound(
                        tag=xray.hopb_inbound_tag(allocation.remote_cascade_id)
                    ),
                ),
                context=f"peering release-exit-link {allocation.id}",
            )
            await db.delete(allocation)
            return pb.ReleaseExitLinkResponse(released=True)

    async def SwitchLinkPreset(
        self, request: pb.SwitchLinkPresetRequest, context: grpc.aio.ServicerContext
    ) -> pb.SwitchLinkPresetResponse:
        async with session_scope() as db:
            allocation = await db.get(ExitLinkAllocation, request.link_id)
            if allocation is None or allocation.peer_platform_id != self._peer_platform_id:
                return pb.SwitchLinkPresetResponse(accepted=False, reason="unknown link_id")

            private_key, public_key = reality_keys.generate_reality_keypair()
            short_id = reality_keys.generate_short_id()
            inbound_tag = xray.hopb_inbound_tag(allocation.remote_cascade_id)

            await dispatch_best_effort(
                allocation.exit_node_id,
                agent_pb.Command(command_id=new_command_id(), remove_inbound=agent_pb.RemoveInbound(tag=inbound_tag)),
                context=f"peering switch-preset {allocation.id} remove old inbound",
            )
            # Preset-aware dispatch lands in slice 11 (build_hopb_inbound_json gaining
            # a `preset` parameter) - this re-dispatches the same P1-only shape with
            # fresh keys for now, so the link_id/key-rotation plumbing is exercised
            # end-to-end even before the transport itself can actually vary.
            inbound_ok = await dispatch_best_effort(
                allocation.exit_node_id,
                agent_pb.Command(
                    command_id=new_command_id(),
                    add_inbound=agent_pb.AddInbound(
                        inbound_json=xray.build_hopb_inbound_json(
                            allocation.remote_cascade_id, allocation.exit_node_id, private_key, short_id
                        )
                    ),
                ),
                context=f"peering switch-preset {allocation.id} add new inbound",
            )

            allocation.active_preset = request.preset
            allocation.hopb_reality_private_key = private_key
            allocation.hopb_reality_public_key = public_key
            allocation.hopb_reality_short_id = short_id
            allocation.status = ExitLinkAllocationStatus.UP if inbound_ok else ExitLinkAllocationStatus.DOWN

            return pb.SwitchLinkPresetResponse(
                accepted=True, hopb_reality_public_key=public_key, hopb_reality_short_id=short_id
            )

    async def ListExitLinks(
        self, request: pb.ListExitLinksRequest, context: grpc.aio.ServicerContext
    ) -> pb.ListExitLinksResponse:
        async with session_scope() as db:
            stmt = select(ExitLinkAllocation).where(ExitLinkAllocation.peer_platform_id == self._peer_platform_id)
            if request.remote_cascade_id:
                stmt = stmt.where(ExitLinkAllocation.remote_cascade_id == request.remote_cascade_id)
            allocations = (await db.execute(stmt)).scalars().all()

            return pb.ListExitLinksResponse(
                links=[
                    pb.ExitLinkSummary(
                        link_id=a.id,
                        exit_node_id=a.exit_node_id,
                        active_preset=a.active_preset,
                        status=_STATUS_TO_PROTO[a.status],
                    )
                    for a in allocations
                ]
            )
