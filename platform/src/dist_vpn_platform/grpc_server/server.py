"""gRPC server bootstrap for the AgentControl service.

TZ-5 §5: bounded message size so one overloaded node can't degrade the others.
TZ-5 §6.2: real mutual TLS - the server presents a leaf cert signed by the Platform's
own CA (security/mtls_ca.py) and requires + verifies a client cert against that same
CA root for every connecting Agent. No plaintext/insecure credentials in this slice
(the old `insecure_server_credentials()` local-dev-only stub is gone - see git history
on this file for the previous version and platform/README.md "Отклонения от ТЗ" #9,
now resolved).
"""
from __future__ import annotations

import logging

import grpc

from dist_vpn_platform.config import settings
from dist_vpn_platform.generated import agent_control_pb2_grpc as pb_grpc
from dist_vpn_platform.grpc_server.servicer import AgentControlServicer
from dist_vpn_platform.security import mtls_ca

logger = logging.getLogger(__name__)


def build_server() -> grpc.aio.Server:
    server = grpc.aio.server(
        options=[
            ("grpc.max_send_message_length", settings.grpc_max_message_bytes),
            ("grpc.max_receive_message_length", settings.grpc_max_message_bytes),
        ]
    )
    pb_grpc.add_AgentControlServicer_to_server(AgentControlServicer(), server)

    # Generates + persists the CA and this server's leaf cert on first startup if
    # nothing exists yet in settings.mtls_ca_dir, otherwise loads what's already there
    # (see security/mtls_ca.py for the full design note - key algorithm, validity
    # periods, persistence layout).
    material = mtls_ca.get_mtls_material()
    credentials = grpc.ssl_server_credentials(
        [(material.server.key_pem, material.server.cert_pem)],
        root_certificates=material.ca.cert_pem,
        require_client_auth=True,  # real mTLS: reject any peer without a cert signed by our CA
    )

    bind_address = f"{settings.grpc_host}:{settings.grpc_port}"
    server.add_secure_port(bind_address, credentials)
    logger.info("grpc_server_configured", extra={"bind_address": bind_address, "tls": "mutual"})
    return server


async def serve() -> None:
    server = build_server()
    await server.start()
    logger.info("grpc_server_started", extra={"port": settings.grpc_port})
    await server.wait_for_termination()
