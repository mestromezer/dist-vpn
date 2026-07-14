"""Minimal in-process HandlerService/RoutingService/StatsService servicers
for exercising XrayClient end-to-end without a real Xray-core binary.

Same pattern as ``mock_platform_server.py`` for ``AgentControl``. Test-only
code -- it implements just enough of each service to record what it
received and return the (empty) response messages the real methods return,
plus a way to force a specific gRPC error to verify error propagation.

IMPORTANT (see agent/README.md and the final-report notes on this slice):
this validates wire-format correctness -- that XrayClient builds messages
matching the real vendored Xray-core .proto shapes and calls the right RPCs
-- but it is not a substitute for testing against a real Xray-core binary.
A mock servicer will happily accept a message a real Xray-core would reject
at a semantic level (e.g. an inbound with a self-contradictory stream
config); this repo's test suite could not run a real Xray-core (see the
final report for what that would take), so that gap remains open.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import grpc.aio

from dist_vpn_agent.generated.xray.app.proxyman.command import command_pb2 as handler_pb2
from dist_vpn_agent.generated.xray.app.proxyman.command import (
    command_pb2_grpc as handler_pb2_grpc,
)
from dist_vpn_agent.generated.xray.app.router.command import command_pb2 as routing_pb2
from dist_vpn_agent.generated.xray.app.router.command import (
    command_pb2_grpc as routing_pb2_grpc,
)
from dist_vpn_agent.generated.xray.app.stats.command import command_pb2 as stats_pb2
from dist_vpn_agent.generated.xray.app.stats.command import command_pb2_grpc as stats_pb2_grpc


class MockHandlerServicer(handler_pb2_grpc.HandlerServiceServicer):
    def __init__(self, fail_with: grpc.StatusCode | None = None, fail_details: str = "") -> None:
        self.fail_with = fail_with
        self.fail_details = fail_details
        self.add_inbound_requests: list[handler_pb2.AddInboundRequest] = []
        self.remove_inbound_requests: list[handler_pb2.RemoveInboundRequest] = []
        self.add_outbound_requests: list[handler_pb2.AddOutboundRequest] = []
        self.remove_outbound_requests: list[handler_pb2.RemoveOutboundRequest] = []
        self.alter_inbound_requests: list[handler_pb2.AlterInboundRequest] = []

    async def _maybe_fail(self, context: grpc.aio.ServicerContext) -> None:
        if self.fail_with is not None:
            await context.abort(self.fail_with, self.fail_details)

    async def AddInbound(self, request, context):  # noqa: N802 - gRPC-generated name
        await self._maybe_fail(context)
        self.add_inbound_requests.append(request)
        return handler_pb2.AddInboundResponse()

    async def RemoveInbound(self, request, context):  # noqa: N802
        await self._maybe_fail(context)
        self.remove_inbound_requests.append(request)
        return handler_pb2.RemoveInboundResponse()

    async def AddOutbound(self, request, context):  # noqa: N802
        await self._maybe_fail(context)
        self.add_outbound_requests.append(request)
        return handler_pb2.AddOutboundResponse()

    async def RemoveOutbound(self, request, context):  # noqa: N802
        await self._maybe_fail(context)
        self.remove_outbound_requests.append(request)
        return handler_pb2.RemoveOutboundResponse()

    async def AlterInbound(self, request, context):  # noqa: N802
        await self._maybe_fail(context)
        self.alter_inbound_requests.append(request)
        return handler_pb2.AlterInboundResponse()

    async def ListInbounds(self, request, context):  # noqa: N802
        return handler_pb2.ListInboundsResponse()

    async def GetInboundUsers(self, request, context):  # noqa: N802
        return handler_pb2.GetInboundUserResponse()

    async def GetInboundUsersCount(self, request, context):  # noqa: N802
        return handler_pb2.GetInboundUsersCountResponse()

    async def AlterOutbound(self, request, context):  # noqa: N802
        return handler_pb2.AlterOutboundResponse()

    async def ListOutbounds(self, request, context):  # noqa: N802
        return handler_pb2.ListOutboundsResponse()


class MockRoutingServicer(routing_pb2_grpc.RoutingServiceServicer):
    def __init__(self, fail_with: grpc.StatusCode | None = None, fail_details: str = "") -> None:
        self.fail_with = fail_with
        self.fail_details = fail_details
        self.add_rule_requests: list[routing_pb2.AddRuleRequest] = []
        self.remove_rule_requests: list[routing_pb2.RemoveRuleRequest] = []
        self.override_balancer_requests: list[routing_pb2.OverrideBalancerTargetRequest] = []

    async def _maybe_fail(self, context: grpc.aio.ServicerContext) -> None:
        if self.fail_with is not None:
            await context.abort(self.fail_with, self.fail_details)

    async def AddRule(self, request, context):  # noqa: N802
        await self._maybe_fail(context)
        self.add_rule_requests.append(request)
        return routing_pb2.AddRuleResponse()

    async def RemoveRule(self, request, context):  # noqa: N802
        await self._maybe_fail(context)
        self.remove_rule_requests.append(request)
        return routing_pb2.RemoveRuleResponse()

    async def OverrideBalancerTarget(self, request, context):  # noqa: N802
        await self._maybe_fail(context)
        self.override_balancer_requests.append(request)
        return routing_pb2.OverrideBalancerTargetResponse()

    async def GetBalancerInfo(self, request, context):  # noqa: N802
        return routing_pb2.GetBalancerInfoResponse()

    async def SubscribeRoutingStats(self, request, context):  # noqa: N802
        return
        yield  # pragma: no cover - makes this an async generator

    async def TestRoute(self, request, context):  # noqa: N802
        return routing_pb2.RoutingContext()

    async def ListRule(self, request, context):  # noqa: N802
        return routing_pb2.ListRuleResponse()


class MockStatsServicer(stats_pb2_grpc.StatsServiceServicer):
    def __init__(self, stats: list[tuple[str, int]] | None = None) -> None:
        self.stats = stats or []
        self.query_stats_requests: list[stats_pb2.QueryStatsRequest] = []

    async def QueryStats(self, request, context):  # noqa: N802
        self.query_stats_requests.append(request)
        return stats_pb2.QueryStatsResponse(
            stat=[stats_pb2.Stat(name=name, value=value) for name, value in self.stats]
        )

    async def GetStats(self, request, context):  # noqa: N802
        return stats_pb2.GetStatsResponse()

    async def GetStatsOnline(self, request, context):  # noqa: N802
        return stats_pb2.GetStatsResponse()

    async def GetSysStats(self, request, context):  # noqa: N802
        return stats_pb2.SysStatsResponse()

    async def GetStatsOnlineIpList(self, request, context):  # noqa: N802
        return stats_pb2.GetStatsOnlineIpListResponse()

    async def GetAllOnlineUsers(self, request, context):  # noqa: N802
        return stats_pb2.GetAllOnlineUsersResponse()

    async def GetUsersStats(self, request, context):  # noqa: N802
        return stats_pb2.GetUsersStatsResponse()


@contextlib.asynccontextmanager
async def running(
    handler: MockHandlerServicer | None = None,
    routing: MockRoutingServicer | None = None,
    stats: MockStatsServicer | None = None,
) -> AsyncIterator[str]:
    """Start a single in-process server hosting whichever mock servicers are
    given, on an ephemeral loopback port; yields "host:port".
    """
    server = grpc.aio.server()
    if handler is not None:
        handler_pb2_grpc.add_HandlerServiceServicer_to_server(handler, server)
    if routing is not None:
        routing_pb2_grpc.add_RoutingServiceServicer_to_server(routing, server)
    if stats is not None:
        stats_pb2_grpc.add_StatsServiceServicer_to_server(stats, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        with contextlib.suppress(Exception):
            await server.stop(None)
