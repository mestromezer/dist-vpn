"""gRPC server bootstrap for the PlatformPeering service - the exit-segment side only
(see contracts/proto/platform_peering.proto's header comment for the fixed dial
direction: RU-Platform is always the client, never this server).

Unlike grpc_server/server.py's Agent-facing listeners (which trust anyone who can
present a client cert chaining to Platform's own CA - a whole fleet), this listener's
`root_certificates` IS the one pinned peer certificate itself, not a CA - only a
connection presenting a client cert matching that exact pinned peer_cert_pem can ever
reach PlatformPeeringServicer's RPCs. See security/peering_identity.py for why this is
cert pinning, not a CA hierarchy.

Building this server requires an async DB read (the PeerPlatform row holding the
pinned material), unlike grpc_server/server.py's build_server() which only reads the
filesystem (security/mtls_ca.py) - hence build_peering_server() is async, called once
from main.py alongside the other two listeners.
"""
from __future__ import annotations

import logging

import grpc
from sqlalchemy import select

from dist_vpn_platform.config import settings
from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import platform_peering_pb2_grpc as pb_grpc
from dist_vpn_platform.grpc_server.peering_servicer import PlatformPeeringServicer
from dist_vpn_platform.models.peer_platform import PeerPlatform

logger = logging.getLogger(__name__)


def _server_options() -> list[tuple[str, int]]:
    return [
        ("grpc.max_send_message_length", settings.grpc_max_message_bytes),
        ("grpc.max_receive_message_length", settings.grpc_max_message_bytes),
    ]


class PeeringNotConfiguredError(RuntimeError):
    """Raised when settings.platform_segment == "exit" but no PeerPlatform row has
    the local+peer certificate material an operator must set up once at provisioning
    time (see models/peer_platform.py, security/peering_identity.py) - fails loudly
    at startup rather than silently not listening, same "explicit preflight error, not
    silent wrong behavior" convention used throughout provisioning/roles/*/tasks/
    preflight.yml."""


async def build_peering_server() -> grpc.aio.Server:
    async with session_scope() as db:
        # In practice there is exactly one PeerPlatform row on the exit-segment side
        # (see models/peer_platform.py's docstring) - nothing in the schema forces
        # that, but the auth mechanism here (one pinned root_certificates per
        # listening port) only supports exactly one peer per port today, matching
        # this slice's two-segment scope.
        peer = (await db.execute(select(PeerPlatform))).scalars().first()

    if peer is None or not peer.local_peering_key_pem or not peer.local_peering_cert_pem or not peer.peer_cert_pem:
        raise PeeringNotConfiguredError(
            "platform_segment='exit' but no PeerPlatform row has local_peering_key_pem/"
            "local_peering_cert_pem/peer_cert_pem set - generate a peering identity "
            "(security/peering_identity.py) and exchange certificates with the RU-segment "
            "operator before starting this Platform instance."
        )

    server = grpc.aio.server(options=_server_options())
    pb_grpc.add_PlatformPeeringServicer_to_server(PlatformPeeringServicer(peer_platform_id=peer.id), server)

    credentials = grpc.ssl_server_credentials(
        [(peer.local_peering_key_pem.encode("ascii"), peer.local_peering_cert_pem.encode("ascii"))],
        root_certificates=peer.peer_cert_pem.encode("ascii"),
        require_client_auth=True,
    )
    bind_address = f"{settings.grpc_host}:{settings.peering_grpc_port}"
    server.add_secure_port(bind_address, credentials)
    logger.info("peering_grpc_server_configured", extra={"bind_address": bind_address, "peer_platform_id": peer.id})
    return server


async def serve() -> None:
    server = await build_peering_server()
    await server.start()
    logger.info("peering_grpc_server_started", extra={"port": settings.peering_grpc_port})
    await server.wait_for_termination()
