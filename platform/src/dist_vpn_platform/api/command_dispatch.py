"""Shared helper for REST route handlers (cascades.py, users.py) that need to fan a
Command out to one or more Agents without letting a single offline/slow node fail the
whole HTTP request - see api/routes/cascades.py and api/routes/users.py docstrings for
why "best effort" is the chosen behavior here (matches the existing
decommission_node()-style "Platform-side registry stays authoritative even if the
Agent-side reconciliation lags" pattern already used in api/routes/nodes.py).
"""
from __future__ import annotations

import logging

from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.grpc_server.servicer import (
    CommandDispatchTimeoutError,
    NodeNotConnectedError,
    dispatch_command,
)

logger = logging.getLogger(__name__)


async def dispatch_best_effort(node_id: str, command: pb.Command, *, context: str) -> bool:
    """Attempt to dispatch `command` to `node_id`'s live Agent session and await its
    CommandResult. Returns True on a successful, `success=True` result; False (with a
    logged warning, never raising) if the node isn't connected, the dispatch timed
    out, or the Agent itself reported failure - callers use the return value to decide
    Cascade/User desired-state bookkeeping (e.g. CascadeStatus), not to abort the
    request.
    """
    try:
        result = await dispatch_command(node_id, command)
    except NodeNotConnectedError:
        logger.warning("command_dispatch_node_offline", extra={"node_id": node_id, "context": context})
        return False
    except CommandDispatchTimeoutError:
        logger.warning("command_dispatch_timeout", extra={"node_id": node_id, "context": context})
        return False

    if not result.success:
        logger.warning(
            "command_dispatch_agent_reported_failure",
            extra={"node_id": node_id, "context": context, "error_message": result.error_message},
        )
        return False
    return True
