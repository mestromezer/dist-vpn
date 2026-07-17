"""gRPC server bootstrap for the AgentControl service.

TZ-5 §5: bounded message size so one overloaded node can't degrade the others.
TZ-5 §6.2: real mutual TLS - the server presents a leaf cert signed by the Platform's
own CA (security/mtls_ca.py) and verifies any client cert against that same CA root.
No plaintext/insecure credentials in this slice (the old `insecure_server_credentials()`
local-dev-only stub is gone - see git history on this file for the previous version and
platform/README.md "Отклонения от ТЗ" #9, now resolved).

Two separate listeners, not one, both serving the same AgentControlServicer:

  - `settings.grpc_port` (default 50051): strict mTLS, `require_client_auth=True`.
    Unchanged from the original design - every interaction except bootstrap Enrollment
    goes through here, and grpc/BoringSSL itself refuses any connection without a
    client cert chaining to our own CA before the RPC handler ever runs.
  - `settings.grpc_enrollment_port` (default 50051 + 1): no client cert requested or
    checked at all. Used only for the Enrollment RPC, authenticated by the one-time
    token in the message body (grpc_server/servicer.py::_handle_enrollment), not by
    TLS identity.

Why two ports instead of one optional-cert port (found while validating a real
two-VDS deployment, not caught by mocked tests - see TESTING.md): a brand-new Agent
has no client cert yet - that's the entire point of the one-time-token Enrollment RPC
- so a single strict port kills the connection before the Agent can even send that
first message. The obvious fix, `require_client_auth=False` on the existing port, was
tried and reverted: confirmed empirically against a real server (not just from docs)
that grpc's C-core does NOT expose an *offered* client cert to the application at all
once client auth isn't required for that port - "request but don't verify" also means
"don't tell the application what was offered, verified or not". That makes it
impossible for the application layer to distinguish "no cert" from "a cert, just
unchecked" on an optional-cert port, which defeats the entire point of checking at the
application layer instead. A second, separate listener with no cert requirement at
all sidesteps the problem rather than fighting it - the same shape of solution RFC
7030 (EST) and Kubernetes bootstrap tokens use (a distinct enrollment channel/endpoint,
authenticated by a one-time credential instead of an established identity).
"""
from __future__ import annotations

import asyncio
import logging

import grpc

from dist_vpn_platform.config import settings
from dist_vpn_platform.generated import agent_control_pb2_grpc as pb_grpc
from dist_vpn_platform.grpc_server.servicer import AgentControlServicer
from dist_vpn_platform.security import mtls_ca

logger = logging.getLogger(__name__)


def _server_options() -> list[tuple[str, int]]:
    return [
        ("grpc.max_send_message_length", settings.grpc_max_message_bytes),
        ("grpc.max_receive_message_length", settings.grpc_max_message_bytes),
    ]


# OWASP API4:2023 (Unrestricted Resource Consumption): the enrollment listener accepts
# a stream from literally anyone with network access, no client cert to even make
# opening one non-trivial - cheaper per-connection than the strict listener by design
# (see module docstring). Per-process/per-connection stream caps are a second layer
# behind the real mitigation (ufw connection-rate limiting on this port at the host
# level, same pattern already used for SSH via fail2ban - see SECRETS-ROTATION.md /
# deploy notes for the actual firewall rule); this alone would not stop a distributed
# flood, but bounds how much of THIS process a single connection can occupy.
_ENROLLMENT_MAX_CONCURRENT_STREAMS = 4


def build_server() -> grpc.aio.Server:
    """Strict mTLS listener - every interaction except Enrollment."""
    server = grpc.aio.server(options=_server_options())
    pb_grpc.add_AgentControlServicer_to_server(AgentControlServicer(enrollment_only=False), server)

    # Generates + persists the CA and this server's leaf cert on first startup if
    # nothing exists yet in settings.mtls_ca_dir, otherwise loads what's already there
    # (see security/mtls_ca.py for the full design note - key algorithm, validity
    # periods, persistence layout). Shared with build_enrollment_server() below - both
    # listeners present the SAME server identity, just with different client-auth
    # policies.
    material = mtls_ca.get_mtls_material()
    credentials = grpc.ssl_server_credentials(
        [(material.server.key_pem, material.server.cert_pem)],
        root_certificates=material.ca.cert_pem,
        require_client_auth=True,
    )

    bind_address = f"{settings.grpc_host}:{settings.grpc_port}"
    server.add_secure_port(bind_address, credentials)
    logger.info("grpc_server_configured", extra={"bind_address": bind_address, "tls": "mutual"})
    return server


def build_enrollment_server() -> grpc.aio.Server:
    """No-client-cert listener, Enrollment only - see module docstring."""
    server = grpc.aio.server(
        options=[*_server_options(), ("grpc.max_concurrent_streams", _ENROLLMENT_MAX_CONCURRENT_STREAMS)]
    )
    pb_grpc.add_AgentControlServicer_to_server(AgentControlServicer(enrollment_only=True), server)

    material = mtls_ca.get_mtls_material()
    # No root_certificates: this port never asks for, let alone checks, a client
    # cert - the one-time enrollment_token is the only credential it accepts, enforced
    # in the application layer (servicer.py::Session, AgentControlServicer.enrollment_only).
    credentials = grpc.ssl_server_credentials(
        [(material.server.key_pem, material.server.cert_pem)],
        require_client_auth=False,
    )

    bind_address = f"{settings.grpc_host}:{settings.grpc_enrollment_port}"
    server.add_secure_port(bind_address, credentials)
    logger.info("grpc_enrollment_server_configured", extra={"bind_address": bind_address, "tls": "server-only"})
    return server


async def serve() -> None:
    server = build_server()
    enrollment_server = build_enrollment_server()
    await asyncio.gather(server.start(), enrollment_server.start())
    logger.info(
        "grpc_server_started",
        extra={"port": settings.grpc_port, "enrollment_port": settings.grpc_enrollment_port},
    )
    await asyncio.gather(server.wait_for_termination(), enrollment_server.wait_for_termination())
