"""End-to-end tests for XrayClient against an in-process mock Xray-core
(HandlerService/RoutingService/StatsService), covering the six real
operations wired up in this slice plus request-translation and gRPC error
propagation.

Scope note (see agent/README.md and the final-report notes on this slice):
these tests verify XrayClient builds wire-correct requests against the real
vendored Xray-core proto shapes and that RPC errors are surfaced, not
swallowed. They do NOT verify a real Xray-core binary accepts these
requests semantically (e.g. that a REALITY private key of the wrong length
is rejected the way real Xray would) -- this sandboxed environment has no
Xray-core binary to test against; see the final report for exactly what
was and wasn't verified against the real thing.
"""

from __future__ import annotations

import json

import grpc
import pytest
from google.protobuf import json_format

from dist_vpn_agent.generated.xray.app.router.command import command_pb2 as routing_pb2
from dist_vpn_agent.generated.xray.app.router import config_pb2 as router_config_pb2
from dist_vpn_agent.xray_client import XrayClient, XrayClientError
from mock_xray_server import MockHandlerServicer, MockRoutingServicer, MockStatsServicer, running

VLESS_REALITY_INBOUND = {
    "tag": "hopA-in-1",
    "listen": "0.0.0.0",
    "port": 443,
    "protocol": "vless",
    "settings": {"decryption": "none"},
    "sniffing": {"enabled": True, "destinationOverride": ["http", "tls"]},
    "streamSettings": {
        "network": "raw",
        "security": "reality",
        "tcpSettings": {},
        "realitySettings": {
            "dest": "example.com:443",
            "serverNames": ["example.com"],
            "privateKey": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "shortIds": ["ab12"],
        },
    },
}

FREEDOM_OUTBOUND = {"tag": "out-1", "protocol": "freedom", "settings": {"domainStrategy": "AS_IS"}}


@pytest.mark.asyncio
async def test_add_inbound_builds_real_handler_config_and_calls_addinbound():
    handler = MockHandlerServicer()
    async with running(handler=handler) as address:
        client = XrayClient(address)
        await client.add_inbound(json.dumps(VLESS_REALITY_INBOUND))
        await client.close()

    assert len(handler.add_inbound_requests) == 1
    inbound = handler.add_inbound_requests[0].inbound
    assert inbound.tag == "hopA-in-1"
    assert inbound.receiver_settings.type == "xray.app.proxyman.ReceiverConfig"
    assert inbound.proxy_settings.type == "xray.proxy.vless.inbound.Config"

    # Decode the nested TypedMessage payloads back out to confirm the real
    # nested proto shapes (stream settings, REALITY settings) round-trip.
    from dist_vpn_agent.generated.xray.app.proxyman import config_pb2 as proxyman_config_pb2
    from dist_vpn_agent.generated.xray.proxy.vless.inbound import config_pb2 as vless_inbound_pb2

    receiver = proxyman_config_pb2.ReceiverConfig()
    receiver.MergeFromString(inbound.receiver_settings.value)
    assert receiver.port_list.range[0].From == 443
    assert receiver.stream_settings.protocol_name == "raw"
    assert receiver.stream_settings.security_type == "xray.transport.internet.reality.Config"
    assert receiver.sniffing_settings.enabled is True
    assert list(receiver.sniffing_settings.destination_override) == ["http", "tls"]

    proxy_settings = vless_inbound_pb2.Config()
    proxy_settings.MergeFromString(inbound.proxy_settings.value)
    assert proxy_settings.decryption == "none"


@pytest.mark.asyncio
async def test_remove_inbound():
    handler = MockHandlerServicer()
    async with running(handler=handler) as address:
        client = XrayClient(address)
        await client.remove_inbound("hopA-in-1")
        await client.close()

    assert len(handler.remove_inbound_requests) == 1
    assert handler.remove_inbound_requests[0].tag == "hopA-in-1"


@pytest.mark.asyncio
async def test_add_outbound_and_remove_outbound():
    handler = MockHandlerServicer()
    async with running(handler=handler) as address:
        client = XrayClient(address)
        await client.add_outbound(json.dumps(FREEDOM_OUTBOUND))
        await client.remove_outbound("out-1")
        await client.close()

    assert len(handler.add_outbound_requests) == 1
    outbound = handler.add_outbound_requests[0].outbound
    assert outbound.tag == "out-1"
    assert outbound.proxy_settings.type == "xray.proxy.freedom.Config"
    assert handler.remove_outbound_requests[0].tag == "out-1"


@pytest.mark.asyncio
async def test_add_user_and_remove_user_via_alter_inbound():
    handler = MockHandlerServicer()
    async with running(handler=handler) as address:
        client = XrayClient(address)
        await client.add_user("hopA-in-1", uuid="66ad4540-b58c-4ad2-9926-ea63445a9b57", email="u@example.com", flow="xtls-rprx-vision")
        await client.remove_user("hopA-in-1", email="u@example.com")
        await client.close()

    assert len(handler.alter_inbound_requests) == 2

    from dist_vpn_agent.generated.xray.app.proxyman.command import command_pb2 as handler_pb2
    from dist_vpn_agent.generated.xray.common.protocol import user_pb2 as protocol_user_pb2
    from dist_vpn_agent.generated.xray.proxy.vless import account_pb2 as vless_account_pb2

    add_request = handler.alter_inbound_requests[0]
    assert add_request.tag == "hopA-in-1"
    add_op = handler_pb2.AddUserOperation()
    add_op.MergeFromString(add_request.operation.value)
    assert add_op.user.email == "u@example.com"
    account = vless_account_pb2.Account()
    account.MergeFromString(add_op.user.account.value)
    assert account.id == "66ad4540-b58c-4ad2-9926-ea63445a9b57"
    assert account.flow == "xtls-rprx-vision"

    remove_request = handler.alter_inbound_requests[1]
    remove_op = handler_pb2.RemoveUserOperation()
    remove_op.MergeFromString(remove_request.operation.value)
    assert remove_op.email == "u@example.com"


@pytest.mark.asyncio
async def test_add_routing_rule_and_remove_routing_rule():
    routing = MockRoutingServicer()
    async with running(routing=routing) as address:
        client = XrayClient(address)
        await client.add_routing_rule(
            "rule-1", json.dumps({"tag": "out-1", "inboundTag": ["hopA-in-1"], "domain": []})
        )
        await client.remove_routing_rule("rule-1")
        await client.close()

    assert len(routing.add_rule_requests) == 1
    add_request = routing.add_rule_requests[0]
    assert add_request.shouldAppend is True
    rule = router_config_pb2.RoutingRule()
    rule.MergeFromString(add_request.config.value)
    assert rule.rule_tag == "rule-1"  # Command's rule_tag wins over rule_json
    assert rule.tag == "out-1"
    assert list(rule.inbound_tag) == ["hopA-in-1"]

    assert routing.remove_rule_requests[0].ruleTag == "rule-1"


@pytest.mark.asyncio
async def test_override_balancer():
    routing = MockRoutingServicer()
    async with running(routing=routing) as address:
        client = XrayClient(address)
        await client.override_balancer("balancer-1", "out-2")
        await client.close()

    request = routing.override_balancer_requests[0]
    assert request.balancerTag == "balancer-1"
    assert request.target == "out-2"


@pytest.mark.asyncio
async def test_override_balancer_empty_target_clears_override():
    routing = MockRoutingServicer()
    async with running(routing=routing) as address:
        client = XrayClient(address)
        await client.override_balancer("balancer-1", "")
        await client.close()

    assert routing.override_balancer_requests[0].target == ""


@pytest.mark.asyncio
async def test_query_stats():
    stats = MockStatsServicer(stats=[("user>>>u@example.com>>>traffic>>>uplink", 1234)])
    async with running(stats=stats) as address:
        client = XrayClient(address)
        result = await client.query_stats(pattern="user>>>")
        await client.close()

    assert result == [("user>>>u@example.com>>>traffic>>>uplink", 1234)]
    assert stats.query_stats_requests[0].pattern == "user>>>"


@pytest.mark.asyncio
async def test_unsupported_protocol_raises_xray_client_error_without_calling_rpc():
    handler = MockHandlerServicer()
    async with running(handler=handler) as address:
        client = XrayClient(address)
        with pytest.raises(XrayClientError, match="unsupported inbound protocol"):
            await client.add_inbound(json.dumps({"protocol": "vmess", "settings": {}}))
        await client.close()

    assert handler.add_inbound_requests == []


@pytest.mark.asyncio
async def test_invalid_json_raises_xray_client_error():
    handler = MockHandlerServicer()
    async with running(handler=handler) as address:
        client = XrayClient(address)
        with pytest.raises(XrayClientError, match="add_inbound"):
            await client.add_inbound("{not valid json")
        await client.close()


@pytest.mark.asyncio
async def test_real_grpc_error_from_xray_propagates_not_swallowed():
    handler = MockHandlerServicer(
        fail_with=grpc.StatusCode.INVALID_ARGUMENT, fail_details="tag already exists"
    )
    async with running(handler=handler) as address:
        client = XrayClient(address)
        with pytest.raises(XrayClientError) as exc_info:
            await client.remove_inbound("dup-tag")
        await client.close()

    message = str(exc_info.value)
    assert "INVALID_ARGUMENT" in message
    assert "tag already exists" in message
    # The original grpc.RpcError must be chained, not swallowed.
    assert isinstance(exc_info.value.__cause__, grpc.RpcError)
