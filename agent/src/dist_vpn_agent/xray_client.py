"""gRPC client to the *local* Xray-core control API.

Wraps the six ``Command`` operations from ``contracts/proto/agent_control.proto``
that TZ-4 SS3.2 maps onto real Xray-core gRPC calls:

| ``Command.op``                    | Xray gRPC call                                         |
|------------------------------------|--------------------------------------------------------|
| ``add_inbound`` / ``remove_inbound``   | ``HandlerService.AddInbound`` / ``RemoveInbound``   |
| ``add_outbound`` / ``remove_outbound`` | ``HandlerService.AddOutbound`` / ``RemoveOutbound`` |
| ``add_user`` / ``remove_user``         | ``HandlerService.AlterInbound`` + Add/RemoveUserOperation |
| ``add_routing_rule`` / ``remove_routing_rule`` | ``RoutingService.AddRule`` / ``RemoveRule`` |
| ``override_balancer``              | ``RoutingService.OverrideBalancerTarget``               |

``run_test`` and ``rotate_secret`` are NOT here -- see control_client.py and
agent/README.md "Scope of this slice" for why (they need the Docker-lifecycle
piece and more design respectively, not just an Xray RPC call).

Connects to the Xray-core container's API over loopback (``xray_api_address``
in config.py, default ``127.0.0.1:10085``). This is a purely local
connection -- either the same host, or the same Docker network namespace as
the Xray-core container this Agent itself started (docker_manager.py) -- and
is never exposed externally, so a plaintext gRPC channel here is not the
security regression that ``insecure_dev_mode`` is for the Agent<->Platform
channel in control_client.py (there is no network hop between untrusted
parties to eavesdrop on or spoof).

--- Real proto shapes, not guessed (see agent/vendor/xray-proto/README.md) ---

Every message type used below is generated from the actual Xray-core
``.proto`` files vendored under ``agent/vendor/xray-proto/``, fetched
directly from XTLS/Xray-core at a pinned tag/commit -- not reconstructed
from memory or documentation.

--- Contract-alignment concern: inbound_json/outbound_json/rule_json shape (not fixed here, flagging only) ---

``agent_control.proto``'s comment on ``AddInbound.inbound_json`` describes it
as "a valid fragment {"inbounds":[{...}]}" -- i.e. Xray's *static config file*
JSON dialect. That dialect is produced/consumed exclusively by Xray-core's Go
``infra/conf`` package (bespoke per-field JSON tags, aliases like
``rawSettings``/``tcpSettings`` both meaning TCP, ``destOverride`` for
sniffing's ``destination_override``, etc.) purely at config-file-load time --
it is *not* what ``HandlerService.AddInbound`` accepts on the wire. That RPC
wants a real ``core.InboundHandlerConfig`` protobuf message, whose
``receiver_settings``/``proxy_settings`` are ``TypedMessage`` envelopes
(type name + serialized bytes) around proxy/transport-specific protobuf
messages. There is no public, non-Go tool that converts Xray's static-config
JSON dialect into those messages; reproducing it faithfully for arbitrary
Xray inbounds would mean re-implementing large, undocumented parts of
``infra/conf`` in Python.

Given this project's scope is bounded to VLESS presets P1-P4 (TZ-4 SS3.3),
this module defines its own small, explicit JSON envelope instead (see
``_build_inbound_handler_config``/``_build_outbound_handler_config``/
``_build_stream_config`` docstrings for the exact shape). It intentionally
*resembles* Xray's static-config keys (``protocol``, ``port``, ``listen``,
``settings``, ``streamSettings``, ``network``, ``security``, ``tcpSettings``/
``grpcSettings``/``xhttpSettings``/``wsSettings``, ``tlsSettings``/
``realitySettings``, ``sniffing``) for familiarity, but the *value* of each
``settings``/``sniffing``/``*Settings`` object is parsed with
``google.protobuf.json_format`` directly against the real vendored proto
message (proto3's own canonical, tool-verified JSON mapping -- lowerCamelCase
field names computed from the real ``.proto``, e.g. ``serviceName``,
``destinationOverride``) rather than Xray's own bespoke Go dialect. This is
a deliberate, scoped bridge, not a guess -- but it is a real divergence from
what the contract comment literally describes, in the same spirit as the
CSR/Enrollment gap flagged in the previous slice's README. It should be
reconciled with the Platform team (either Platform's ``inbound_json``
producer targets this dialect, or the contract comment gets corrected/
expanded) rather than silently assumed correct forever.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
from collections.abc import AsyncIterator
from typing import Any

import grpc
from google.protobuf import json_format
from google.protobuf.message import Message

from dist_vpn_agent.generated.xray.app.proxyman import config_pb2 as proxyman_config_pb2
from dist_vpn_agent.generated.xray.app.proxyman.command import command_pb2 as handler_pb2
from dist_vpn_agent.generated.xray.app.proxyman.command import (
    command_pb2_grpc as handler_pb2_grpc,
)
from dist_vpn_agent.generated.xray.app.router import config_pb2 as router_config_pb2
from dist_vpn_agent.generated.xray.app.router.command import command_pb2 as routing_pb2
from dist_vpn_agent.generated.xray.app.router.command import (
    command_pb2_grpc as routing_pb2_grpc,
)
from dist_vpn_agent.generated.xray.app.stats.command import command_pb2 as stats_pb2
from dist_vpn_agent.generated.xray.app.stats.command import command_pb2_grpc as stats_pb2_grpc
from dist_vpn_agent.generated.xray.common.protocol import user_pb2 as protocol_user_pb2
from dist_vpn_agent.generated.xray.common.serial import typed_message_pb2 as serial_pb2
from dist_vpn_agent.generated.xray.core import config_pb2 as core_config_pb2
from dist_vpn_agent.generated.xray.proxy.freedom import config_pb2 as freedom_config_pb2
from dist_vpn_agent.generated.xray.proxy.vless import account_pb2 as vless_account_pb2
from dist_vpn_agent.generated.xray.proxy.vless.inbound import config_pb2 as vless_inbound_pb2
from dist_vpn_agent.generated.xray.transport.internet import config_pb2 as ti_config_pb2
from dist_vpn_agent.generated.xray.transport.internet.grpc import (
    config_pb2 as grpc_transport_pb2,
)
from dist_vpn_agent.generated.xray.transport.internet.reality import (
    config_pb2 as reality_pb2,
)
from dist_vpn_agent.generated.xray.transport.internet.splithttp import (
    config_pb2 as xhttp_transport_pb2,
)
from dist_vpn_agent.generated.xray.transport.internet.tcp import config_pb2 as tcp_transport_pb2
from dist_vpn_agent.generated.xray.transport.internet.tls import config_pb2 as tls_pb2
from dist_vpn_agent.generated.xray.transport.internet.websocket import (
    config_pb2 as ws_transport_pb2,
)

# Proxy protocols this Agent knows how to translate JSON settings for.
# Scoped deliberately to what TZ-4 SS3.3's presets need (VLESS inbound,
# freedom outbound for local egress) -- see module docstring.
_INBOUND_PROXY_SETTINGS: dict[str, type[Message]] = {
    "vless": vless_inbound_pb2.Config,
}
_OUTBOUND_PROXY_SETTINGS: dict[str, type[Message]] = {
    "freedom": freedom_config_pb2.Config,
}

# Transport ("network") settings, keyed the same way Xray's own JSON dialect
# names them. The third tuple element is the string Xray's transport
# registry actually expects in StreamConfig/TransportConfig.protocol_name --
# confirmed by reading the `const protocolName = "..."` in each transport
# package's Go source (transport/internet/{tcp,grpc,websocket,splithttp}/*.go)
# and cross-checked against a real Xray-core container, NOT guessed: it is
# *not* always the same as our own JSON dialect's network name -- "raw" is
# Xray's newer JSON-config alias for what the transport registry still calls
# "tcp"; "xhttp" (this project's/TZ-4's name for the transport, matching
# current Xray JSON config docs) is registered as "splithttp" (the
# transport's original/internal name, predating the "xhttp" rebrand); "ws"
# is registered as "websocket". Using the JSON-facing name instead of this
# real registered name fails at Xray's runtime listener-registry lookup
# with `transport/internet: <name> listener not registered`, not at request
# validation -- this exact bug was caught by testing against a real
# Xray-core binary (see the final report for this slice), not by the mock
# server, which happily accepts any string here.
_TRANSPORT_SETTINGS: dict[str, tuple[type[Message], str, str]] = {
    "tcp": (tcp_transport_pb2.Config, "tcpSettings", "tcp"),
    "raw": (tcp_transport_pb2.Config, "tcpSettings", "tcp"),
    "grpc": (grpc_transport_pb2.Config, "grpcSettings", "grpc"),
    "xhttp": (xhttp_transport_pb2.Config, "xhttpSettings", "splithttp"),
    "ws": (ws_transport_pb2.Config, "wsSettings", "websocket"),
}

# Stream security settings -- P1-P4 only ever use "none", "tls", or "reality".
_SECURITY_SETTINGS: dict[str, tuple[type[Message], str]] = {
    "tls": (tls_pb2.Config, "tlsSettings"),
    "reality": (reality_pb2.Config, "realitySettings"),
}


class XrayClientError(RuntimeError):
    """Raised for both request-translation failures (bad JSON, unsupported
    protocol/transport/security name) and real gRPC failures talking to
    Xray-core, so control_client.py has exactly one exception type to catch
    and turn into ``CommandResult(success=False, error_message=...)``.

    The underlying cause (a ``grpc.RpcError``, a ``KeyError`` for an
    unsupported name, a ``json_format.ParseError`` for malformed settings,
    ...) is always chained via ``raise ... from err`` -- never swallowed --
    so logs/tracebacks still show exactly what failed and why.
    """


def _typed_message(message: Message) -> serial_pb2.TypedMessage:
    """Wrap ``message`` the way Xray's own ``common/serial.ToTypedMessage``
    does: type name (from the real compiled descriptor, never hand-typed)
    plus serialized bytes.
    """
    return serial_pb2.TypedMessage(type=message.DESCRIPTOR.full_name, value=message.SerializeToString())


def _parse_settings(data: dict[str, Any], message_cls: type[Message]) -> Message:
    message = message_cls()
    try:
        json_format.ParseDict(data, message, ignore_unknown_fields=False)
    except json_format.ParseError as err:
        raise ValueError(f"invalid {message_cls.DESCRIPTOR.full_name} settings: {err}") from err
    return message


def _set_ip_or_domain(target: Any, value: str) -> None:
    """Populate an ``IPOrDomain`` oneof from a plain string: a real IP
    literal becomes the packed-bytes ``ip`` branch (what Xray expects),
    anything else is treated as ``domain``.
    """
    try:
        target.ip = ipaddress.ip_address(value).packed
    except ValueError:
        target.domain = value


def _build_stream_config(data: dict[str, Any]) -> ti_config_pb2.StreamConfig:
    """Build a real ``transport.internet.StreamConfig`` from the Agent's
    ``streamSettings`` envelope (see module docstring for the exact shape):
    ``{"network": "raw"|"grpc"|"xhttp"|"ws", "security": "none"|"tls"|"reality",
    "<network>Settings": {...}, "<security>Settings": {...}}``.
    """
    stream = ti_config_pb2.StreamConfig()

    network = data.get("network", "tcp")
    try:
        transport_cls, settings_key, registered_name = _TRANSPORT_SETTINGS[network]
    except KeyError:
        raise ValueError(
            f"unsupported stream network {network!r} (supported: {sorted(_TRANSPORT_SETTINGS)})"
        ) from None
    stream.protocol_name = registered_name
    transport_message = _parse_settings(data.get(settings_key, {}), transport_cls)
    transport_entry = stream.transport_settings.add()
    transport_entry.protocol_name = registered_name
    transport_entry.settings.CopyFrom(_typed_message(transport_message))

    security = data.get("security", "none")
    if security and security != "none":
        try:
            security_cls, security_key = _SECURITY_SETTINGS[security]
        except KeyError:
            raise ValueError(
                f"unsupported stream security {security!r} "
                f"(supported: none, {sorted(_SECURITY_SETTINGS)})"
            ) from None
        security_message = _parse_settings(data.get(security_key, {}), security_cls)
        stream.security_type = security_cls.DESCRIPTOR.full_name
        stream.security_settings.append(_typed_message(security_message))

    return stream


def _build_inbound_handler_config(data: dict[str, Any]) -> core_config_pb2.InboundHandlerConfig:
    """Build a real ``core.InboundHandlerConfig`` from the Agent's
    ``inbound_json`` envelope::

        {
          "tag": "hopA-in-1",
          "listen": "0.0.0.0",             # optional; IP literal or domain
          "port": 443,                      # optional (some inbounds are portless)
          "protocol": "vless",              # dispatches into _INBOUND_PROXY_SETTINGS
          "settings": {...},                 # proto3-JSON of that protocol's Config
          "sniffing": {...},                 # optional, proto3-JSON of SniffingConfig
          "streamSettings": {...}            # optional, see _build_stream_config
        }

    See the module docstring for why this is *not* Xray's static-config-file
    JSON dialect despite resembling its key names.
    """
    try:
        protocol = data["protocol"]
    except KeyError:
        raise ValueError("inbound_json: missing required 'protocol'") from None
    try:
        proxy_cls = _INBOUND_PROXY_SETTINGS[protocol]
    except KeyError:
        raise ValueError(
            f"unsupported inbound protocol {protocol!r} (supported: {sorted(_INBOUND_PROXY_SETTINGS)})"
        ) from None
    proxy_message = _parse_settings(data.get("settings", {}), proxy_cls)

    receiver = proxyman_config_pb2.ReceiverConfig()
    if "port" in data:
        port = int(data["port"])
        receiver.port_list.range.add(From=port, To=port)
    if "listen" in data:
        _set_ip_or_domain(receiver.listen, data["listen"])
    if "sniffing" in data:
        sniffing = _parse_settings(data["sniffing"], proxyman_config_pb2.SniffingConfig)
        receiver.sniffing_settings.CopyFrom(sniffing)
    if "streamSettings" in data:
        receiver.stream_settings.CopyFrom(_build_stream_config(data["streamSettings"]))

    return core_config_pb2.InboundHandlerConfig(
        tag=data.get("tag", ""),
        receiver_settings=_typed_message(receiver),
        proxy_settings=_typed_message(proxy_message),
    )


def _build_outbound_handler_config(data: dict[str, Any]) -> core_config_pb2.OutboundHandlerConfig:
    """Build a real ``core.OutboundHandlerConfig`` from the Agent's
    ``outbound_json`` envelope -- same spirit as ``_build_inbound_handler_config``::

        {"tag": "...", "protocol": "freedom", "settings": {...}, "streamSettings": {...}}
    """
    try:
        protocol = data["protocol"]
    except KeyError:
        raise ValueError("outbound_json: missing required 'protocol'") from None
    try:
        proxy_cls = _OUTBOUND_PROXY_SETTINGS[protocol]
    except KeyError:
        raise ValueError(
            f"unsupported outbound protocol {protocol!r} (supported: {sorted(_OUTBOUND_PROXY_SETTINGS)})"
        ) from None
    proxy_message = _parse_settings(data.get("settings", {}), proxy_cls)

    sender = proxyman_config_pb2.SenderConfig()
    if "streamSettings" in data:
        sender.stream_settings.CopyFrom(_build_stream_config(data["streamSettings"]))

    return core_config_pb2.OutboundHandlerConfig(
        tag=data.get("tag", ""),
        sender_settings=_typed_message(sender),
        proxy_settings=_typed_message(proxy_message),
    )


def _describe_rpc_error(err: grpc.RpcError) -> str:
    code = err.code() if hasattr(err, "code") else None
    details = err.details() if hasattr(err, "details") else str(err)
    return f"{code.name if code else '<no code>'}: {details}"


@contextlib.asynccontextmanager
async def _translate_grpc_errors(operation: str) -> AsyncIterator[None]:
    try:
        yield
    except grpc.RpcError as err:
        raise XrayClientError(f"Xray {operation} failed: {_describe_rpc_error(err)}") from err


class XrayClient:
    """One lazily-connected gRPC channel to the local Xray-core control API."""

    def __init__(self, xray_api_address: str) -> None:
        self._address = xray_api_address
        self._channel: grpc.aio.Channel | None = None

    def _ensure_channel(self) -> grpc.aio.Channel:
        if self._channel is None:
            self._channel = grpc.aio.insecure_channel(self._address)
        return self._channel

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    # -- HandlerService (inbound/outbound/user) -------------------------------

    async def add_inbound(self, inbound_json: str) -> None:
        try:
            data = json.loads(inbound_json)
            handler_config = _build_inbound_handler_config(data)
        except (json.JSONDecodeError, ValueError) as err:
            raise XrayClientError(f"add_inbound: {err}") from err

        stub = handler_pb2_grpc.HandlerServiceStub(self._ensure_channel())
        async with _translate_grpc_errors("HandlerService.AddInbound"):
            await stub.AddInbound(handler_pb2.AddInboundRequest(inbound=handler_config))

    async def remove_inbound(self, tag: str) -> None:
        stub = handler_pb2_grpc.HandlerServiceStub(self._ensure_channel())
        async with _translate_grpc_errors("HandlerService.RemoveInbound"):
            await stub.RemoveInbound(handler_pb2.RemoveInboundRequest(tag=tag))

    async def add_outbound(self, outbound_json: str) -> None:
        try:
            data = json.loads(outbound_json)
            handler_config = _build_outbound_handler_config(data)
        except (json.JSONDecodeError, ValueError) as err:
            raise XrayClientError(f"add_outbound: {err}") from err

        stub = handler_pb2_grpc.HandlerServiceStub(self._ensure_channel())
        async with _translate_grpc_errors("HandlerService.AddOutbound"):
            await stub.AddOutbound(handler_pb2.AddOutboundRequest(outbound=handler_config))

    async def remove_outbound(self, tag: str) -> None:
        stub = handler_pb2_grpc.HandlerServiceStub(self._ensure_channel())
        async with _translate_grpc_errors("HandlerService.RemoveOutbound"):
            await stub.RemoveOutbound(handler_pb2.RemoveOutboundRequest(tag=tag))

    async def add_user(self, inbound_tag: str, uuid: str, email: str, flow: str) -> None:
        account = vless_account_pb2.Account(id=uuid, flow=flow)
        user = protocol_user_pb2.User(email=email, account=_typed_message(account))
        operation = handler_pb2.AddUserOperation(user=user)
        await self._alter_inbound(inbound_tag, operation, "HandlerService.AlterInbound(AddUserOperation)")

    async def remove_user(self, inbound_tag: str, email: str) -> None:
        operation = handler_pb2.RemoveUserOperation(email=email)
        await self._alter_inbound(
            inbound_tag, operation, "HandlerService.AlterInbound(RemoveUserOperation)"
        )

    async def _alter_inbound(self, tag: str, operation: Message, description: str) -> None:
        stub = handler_pb2_grpc.HandlerServiceStub(self._ensure_channel())
        request = handler_pb2.AlterInboundRequest(tag=tag, operation=_typed_message(operation))
        async with _translate_grpc_errors(description):
            await stub.AlterInbound(request)

    # -- RoutingService (rules / balancer) -------------------------------------

    async def add_routing_rule(self, rule_tag: str, rule_json: str) -> None:
        try:
            data = json.loads(rule_json)
            rule = router_config_pb2.RoutingRule()
            json_format.ParseDict(data, rule, ignore_unknown_fields=False)
        except (json.JSONDecodeError, json_format.ParseError) as err:
            raise XrayClientError(f"add_routing_rule: invalid rule_json: {err}") from err
        # rule_tag is the Command's own identifying field (also used by
        # remove_routing_rule below) -- it wins over whatever rule_json says,
        # so Add/Remove always agree on the tag.
        rule.rule_tag = rule_tag

        stub = routing_pb2_grpc.RoutingServiceStub(self._ensure_channel())
        request = routing_pb2.AddRuleRequest(config=_typed_message(rule), shouldAppend=True)
        async with _translate_grpc_errors("RoutingService.AddRule"):
            await stub.AddRule(request)

    async def remove_routing_rule(self, rule_tag: str) -> None:
        stub = routing_pb2_grpc.RoutingServiceStub(self._ensure_channel())
        async with _translate_grpc_errors("RoutingService.RemoveRule"):
            await stub.RemoveRule(routing_pb2.RemoveRuleRequest(ruleTag=rule_tag))

    async def override_balancer(self, balancer_tag: str, target_outbound_tag: str) -> None:
        stub = routing_pb2_grpc.RoutingServiceStub(self._ensure_channel())
        request = routing_pb2.OverrideBalancerTargetRequest(
            balancerTag=balancer_tag, target=target_outbound_tag
        )
        async with _translate_grpc_errors("RoutingService.OverrideBalancerTarget"):
            await stub.OverrideBalancerTarget(request)

    # -- StatsService -----------------------------------------------------------
    #
    # Wired up (real stub, real message types) but not called from anywhere
    # yet -- no Command variant needs it this slice. Kept minimal and here
    # for TelemetryReport (TZ-4 SS3.5), a follow-up slice; see agent/README.md.

    async def query_stats(self, pattern: str = "", reset: bool = False) -> list[tuple[str, int]]:
        stub = stats_pb2_grpc.StatsServiceStub(self._ensure_channel())
        request = stats_pb2.QueryStatsRequest(pattern=pattern, reset=reset)
        async with _translate_grpc_errors("StatsService.QueryStats"):
            response = await stub.QueryStats(request)
        return [(stat.name, stat.value) for stat in response.stat]
