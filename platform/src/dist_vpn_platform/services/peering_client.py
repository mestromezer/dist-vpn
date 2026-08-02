"""Thin async client wrapper around the PlatformPeering gRPC stub - the entry-segment
(RU-Platform) side only. Same "thin wrapper class around a generated stub" shape as
agent/src/dist_vpn_agent/xray_client.py is for the local Xray-core API - the closest
existing analog in this codebase.

Per contracts/proto/platform_peering.proto's fixed dial direction, RU-Platform is
always the client here - services/link_negotiation.py (slice 12) is the only intended
caller. Auth is mutual cert pinning (security/peering_identity.py), not the Agent-
facing mTLS CA - see that module and platform_peering.proto's header comment for why.
"""
from __future__ import annotations

import logging

import grpc

from dist_vpn_platform.generated import platform_peering_pb2 as pb
from dist_vpn_platform.generated import platform_peering_pb2_grpc as pb_grpc
from dist_vpn_platform.models.peer_platform import PeerPlatform

logger = logging.getLogger(__name__)


class PeeringNotConfiguredError(RuntimeError):
    """Raised when a PeerPlatform row is missing the local/peer certificate material
    or grpc_address needed to dial - same "explicit error, not a silent no-op"
    convention as grpc_server/peering_server.py's PeeringNotConfiguredError."""


class PeeringClient:
    """One instance per PeerPlatform row - holds a lazily-created channel open across
    calls (services/link_negotiation.py calls this repeatedly on a scheduler interval,
    see services/health_scheduler.py for the polling-loop shape it will follow), not
    re-dialed per RPC. Call `close()` when done with this peer (process shutdown, or
    the peer's own credentials changed and a fresh channel is needed)."""

    def __init__(self, peer: PeerPlatform) -> None:
        if not peer.grpc_address:
            raise PeeringNotConfiguredError(f"PeerPlatform {peer.id} has no grpc_address set")
        if not peer.local_peering_key_pem or not peer.local_peering_cert_pem or not peer.peer_cert_pem:
            raise PeeringNotConfiguredError(f"PeerPlatform {peer.id} is missing local/peer certificate material")

        credentials = grpc.ssl_channel_credentials(
            root_certificates=peer.peer_cert_pem.encode("ascii"),
            private_key=peer.local_peering_key_pem.encode("ascii"),
            certificate_chain=peer.local_peering_cert_pem.encode("ascii"),
        )
        self._channel = grpc.aio.secure_channel(peer.grpc_address, credentials)
        self._stub = pb_grpc.PlatformPeeringStub(self._channel)
        self.peer_platform_id = peer.id

    async def close(self) -> None:
        await self._channel.close()

    async def provision_exit_link(
        self, *, remote_cascade_id: str, exit_node_id: str, initial_preset: str, timeout: float = 10.0
    ) -> pb.ProvisionExitLinkResponse:
        return await self._stub.ProvisionExitLink(
            pb.ProvisionExitLinkRequest(
                remote_cascade_id=remote_cascade_id, exit_node_id=exit_node_id, initial_preset=initial_preset
            ),
            timeout=timeout,
        )

    async def release_exit_link(self, *, link_id: str, timeout: float = 10.0) -> bool:
        response = await self._stub.ReleaseExitLink(pb.ReleaseExitLinkRequest(link_id=link_id), timeout=timeout)
        return response.released

    async def switch_link_preset(
        self, *, link_id: str, preset: str, timeout: float = 10.0
    ) -> pb.SwitchLinkPresetResponse:
        return await self._stub.SwitchLinkPreset(
            pb.SwitchLinkPresetRequest(link_id=link_id, preset=preset), timeout=timeout
        )

    async def list_exit_links(
        self, *, remote_cascade_id: str = "", timeout: float = 10.0
    ) -> list[pb.ExitLinkSummary]:
        response = await self._stub.ListExitLinks(
            pb.ListExitLinksRequest(remote_cascade_id=remote_cascade_id), timeout=timeout
        )
        return list(response.links)
