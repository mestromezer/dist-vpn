"""gRPC server bootstrap for the AgentControl service.

TZ-5 §5: bounded message size so one overloaded node can't degrade the others.
TZ-5 §6.2: real mutual TLS - the server presents a leaf cert signed by the Platform's
own CA (security/mtls_ca.py) and verifies any client cert against that same CA root.
No plaintext/insecure credentials in this slice (the old `insecure_server_credentials()`
local-dev-only stub is gone - see git history on this file for the previous version and
platform/README.md "Отклонения от ТЗ" #9, now resolved).

Found while validating a real two-VDS deployment (not caught by mocked tests - see
TESTING.md): `require_client_auth=True` made bootstrap enrollment structurally
impossible. A brand-new Agent has no client cert yet - that's the entire point of the
one-time-token Enrollment RPC (grpc_server/servicer.py::_handle_enrollment, which
authenticates the token from the message body, not from TLS peer identity) - so a
transport layer that unconditionally demands a client cert kills the connection before
the Agent can even send that first message. This is the same shape of problem RFC 7030
(EST) and Kubernetes bootstrap tokens solve the same way: TLS with an OPTIONAL client
cert at the transport layer, plus an application-layer one-time-token check for the one
RPC that's allowed to proceed without one.

Also found while wiring this up (confirmed empirically, not just from docs - see CI
results that shaped this comment): grpc's C-core does NOT chain-verify an *offered*
client cert against `root_certificates` when `require_client_auth=False` - that mode is
"request the cert but don't verify it", not "verify it if offered". Only
`require_client_auth=True` gets chain verification from grpc/BoringSSL itself. So this
change moves ALL client-cert trust decisions to the application layer:
grpc_server/servicer.py::Session pulls whatever PEM cert `context.auth_context()`
reports for the peer (if any) and verifies it itself against our own CA
(security/mtls_ca.py::verify_client_cert_pem) before treating the connection as
certified - and only a certified connection may send anything other than Enrollment.
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
        # Optional, not required, at the transport layer - see module docstring above
        # for why. A presented cert is still verified against root_certificates; only
        # an ABSENT cert is now allowed through to the application layer, where
        # servicer.py::Session restricts what an uncertified peer may do to Enrollment.
        require_client_auth=False,
    )

    bind_address = f"{settings.grpc_host}:{settings.grpc_port}"
    server.add_secure_port(bind_address, credentials)
    logger.info("grpc_server_configured", extra={"bind_address": bind_address, "tls": "mutual-optional"})
    return server


async def serve() -> None:
    server = build_server()
    await server.start()
    logger.info("grpc_server_started", extra={"port": settings.grpc_port})
    await server.wait_for_termination()
