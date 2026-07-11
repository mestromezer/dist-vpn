"""gRPC server bootstrap for the AgentControl service.

TZ-5 §5: bounded message size so one overloaded node can't degrade the others.
TZ-5 §6.2: production requires mTLS via step-ca; this slice runs plaintext/insecure
credentials for local dev - see platform/README.md TODO for wiring `grpc.ssl_server_credentials`.
"""
from __future__ import annotations

import logging

import grpc

from dist_vpn_platform.config import settings
from dist_vpn_platform.generated import agent_control_pb2_grpc as pb_grpc
from dist_vpn_platform.grpc_server.servicer import AgentControlServicer

logger = logging.getLogger(__name__)


def build_server() -> grpc.aio.Server:
    server = grpc.aio.server(
        options=[
            ("grpc.max_send_message_length", settings.grpc_max_message_bytes),
            ("grpc.max_receive_message_length", settings.grpc_max_message_bytes),
        ]
    )
    pb_grpc.add_AgentControlServicer_to_server(AgentControlServicer(), server)

    # TODO(TZ-5 §6.2): replace insecure_server_credentials with mTLS
    # (grpc.ssl_server_credentials + step-ca-issued cert/key + client CA for
    # require_client_auth) before this ever talks to a real Agent outside of dev.
    bind_address = f"{settings.grpc_host}:{settings.grpc_port}"
    server.add_insecure_port(bind_address)
    logger.info("grpc_server_configured", extra={"bind_address": bind_address})
    return server


async def serve() -> None:
    server = build_server()
    await server.start()
    logger.info("grpc_server_started", extra={"port": settings.grpc_port})
    await server.wait_for_termination()
