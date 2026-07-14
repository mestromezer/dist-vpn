"""End-to-end tests for XrayClient.

Two tiers, mirroring tests/test_docker_manager.py's split -- be explicit
about which is which (see the final report for this slice):

1. Against an in-process mock Xray-core (HandlerService/RoutingService/
   StatsService, tests/mock_xray_server.py), covering all nine dispatched
   operations plus request-translation and gRPC error propagation. These
   verify XrayClient builds wire-correct requests against the real vendored
   Xray-core proto shapes and that RPC errors are surfaced, not swallowed --
   but the mock never decodes/validates a TypedMessage payload's semantics,
   so it cannot catch a request that is wire-valid but means the wrong
   thing to real Xray. These always run.

2. Against a genuine `ghcr.io/xtls/xray-core` container (real integration
   tier at the bottom of this file, skipped with a clear reason if Docker is
   unreachable) -- this is what actually caught two real bugs during this
   slice's development that tier 1 could not have caught: the transport-
   registry name mismatch (see _TRANSPORT_SETTINGS's comment in
   xray_client.py -- "raw"/"xhttp"/"ws" are this project's JSON-dialect
   names, not what Xray's transport registry is keyed by) and
   AddRuleRequest.config needing a full router.Config wrapper rather than a
   bare RoutingRule (see add_routing_rule's comment) -- both found by
   actually running the real binary, not guessed or found by reading Go
   source alone. Semantics not covered by either tier: e.g. whether a
   REALITY private key of the wrong length is rejected the way real Xray
   would -- these tests don't attempt that; they check that translation
   happens correctly, not that every combination of settings is valid to
   Xray's own deeper checks.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import time

import grpc
import pytest
from google.protobuf import json_format

from dist_vpn_agent.config import AgentSettings
from dist_vpn_agent.generated.xray.app.router.command import command_pb2 as routing_pb2
from dist_vpn_agent.generated.xray.app.router import config_pb2 as router_config_pb2
from dist_vpn_agent.xray_bootstrap import ensure_base_config
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
    # "raw" is our JSON dialect's name for this transport; "tcp" is the name
    # Xray's transport registry actually expects (see the long comment on
    # _TRANSPORT_SETTINGS in xray_client.py -- this distinction was found by
    # testing against a real Xray-core binary, not guessed).
    assert receiver.stream_settings.protocol_name == "tcp"
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
    # AddRuleRequest.config wraps a router.Config (domain_strategy/rule/
    # balancing_rule), not a bare RoutingRule -- Xray-core's real
    # Router.AddRule (Go) type-asserts on *Config specifically and rejects
    # anything else with the literal error "AddRule: config type error".
    # Found by testing against a real Xray-core container (see
    # add_routing_rule's comment in xray_client.py); the mock server here
    # doesn't decode the TypedMessage payload at all, so it would have
    # happily accepted the old (wrong) bare-RoutingRule version too.
    assert add_request.config.type == router_config_pb2.Config.DESCRIPTOR.full_name
    config = router_config_pb2.Config()
    config.MergeFromString(add_request.config.value)
    assert len(config.rule) == 1
    rule = config.rule[0]
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


# --- Real Xray-core integration tier -----------------------------------------
#
# See the module docstring for why this exists alongside the mock-based
# tests above. Spins up a genuine ghcr.io/xtls/xray-core container (the same
# image docker_manager.py manages in production) with its control API
# exposed on loopback, runs XrayClient's real operations against it, and
# tears the container down afterward -- skipped with a clear reason if no
# real Docker Engine is reachable, the same pattern
# tests/test_docker_manager.py uses for its real-Docker tier.

_REAL_XRAY_CONTAINER_NAME = "dist-vpn-agent-test-xray-core-client"
_REAL_XRAY_HOST_PORT = 19998


@pytest.fixture
def real_xray_address(tmp_path):
    docker = pytest.importorskip("docker", reason="docker-py not installed")
    from docker.errors import DockerException, NotFound  # noqa: PLC0415

    try:
        client = docker.from_env()
        client.ping()
    except DockerException as err:
        pytest.skip(f"no real Docker Engine available in this environment: {err}")

    try:
        client.containers.get(_REAL_XRAY_CONTAINER_NAME).remove(force=True)
    except NotFound:
        pass

    config_path = tmp_path / "config.json"
    # Real production code path, not a hand-duplicated fixture (was drifting
    # from xray_bootstrap.py's actual output until this slice) - this is the
    # only thing standing between "unit tests pass" and "the generated base
    # config actually lets a real Xray-core container come up with a working
    # api/stats/policy setup", which is exactly what xray_bootstrap.py's own
    # docstring flags as previously never verified end to end.
    ensure_base_config(
        AgentSettings(
            insecure_dev_mode=True,
            xray_config_host_path=config_path,
            xray_api_address=f"127.0.0.1:{_REAL_XRAY_HOST_PORT}",
        )
    )

    container = client.containers.run(
        "ghcr.io/xtls/xray-core:latest",
        name=_REAL_XRAY_CONTAINER_NAME,
        command=["run", "-config", "/etc/xray/config.json"],
        volumes={str(config_path): {"bind": "/etc/xray/config.json", "mode": "ro"}},
        ports={f"{_REAL_XRAY_HOST_PORT}/tcp": ("127.0.0.1", _REAL_XRAY_HOST_PORT)},
        detach=True,
    )
    try:
        yield f"127.0.0.1:{_REAL_XRAY_HOST_PORT}"
    finally:
        container.remove(force=True)


async def _wait_until_ready(client: XrayClient, timeout: float = 15.0) -> None:
    """Real Xray-core takes a moment to bind its API listener after the
    container is technically 'running' -- retry a cheap, always-valid
    operation (add+remove a freedom outbound) until it stops erroring, or
    give up after ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await client.add_outbound(json.dumps({"tag": "readiness-probe", "protocol": "freedom", "settings": {}}))
            await client.remove_outbound("readiness-probe")
            return
        except XrayClientError as err:
            last_err = err
            await asyncio.sleep(0.5)
    raise AssertionError(f"real Xray-core never became ready within {timeout}s: {last_err}")


@pytest.mark.asyncio
async def test_real_xray_core_add_remove_inbound_all_transports(real_xray_address):
    """The exact transport-registry-name translation this slice fixed
    (raw->tcp, xhttp->splithttp, ws->websocket), against a real listener
    registry -- this is precisely the class of bug the mock cannot catch,
    since it fails at Xray's runtime listener lookup, not request shape.
    """
    client = XrayClient(real_xray_address)
    await _wait_until_ready(client)
    try:
        for network, extra_settings in [
            ("raw", {}),
            ("xhttp", {"xhttpSettings": {"path": "/verify"}}),
            ("ws", {"wsSettings": {"path": "/verify-ws"}}),
        ]:
            tag = f"verify-{network}"
            inbound = {
                "tag": tag,
                "listen": "0.0.0.0",
                "port": 21000 + hash(network) % 500,
                "protocol": "vless",
                "settings": {"users": [], "decryption": "none"},
                "streamSettings": {"network": network, "security": "none", **extra_settings},
            }
            await client.add_inbound(json.dumps(inbound))
            await client.remove_inbound(tag)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_xray_core_add_remove_user_via_alter_inbound(real_xray_address):
    client = XrayClient(real_xray_address)
    await _wait_until_ready(client)
    try:
        inbound = {
            "tag": "verify-users",
            "listen": "0.0.0.0",
            "port": 21500,
            "protocol": "vless",
            "settings": {"users": [], "decryption": "none"},
            "streamSettings": {"network": "raw", "security": "none"},
        }
        await client.add_inbound(json.dumps(inbound))
        await client.add_user(
            "verify-users", uuid="b831381d-6324-4d53-ad4f-8cda48b30811", email="verify@example.com", flow=""
        )
        await client.remove_user("verify-users", email="verify@example.com")
        await client.remove_inbound("verify-users")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_xray_core_get_users_stats_no_traffic_yet(real_xray_address):
    """Real proof that xray_bootstrap.py's generated policy/stats config actually
    lets the StatsService RPC chain get_users_stats() uses succeed end to end - the
    real_xray_address fixture above now runs through ensure_base_config() (real
    production code), not a hand-duplicated fixture, so this is direct evidence the
    previously-missing config gap (see xray_bootstrap.py's module docstring) is
    closed, not just that the unit tests believe the right JSON shape was written.
    No client connects in this test (no real traffic/online IPs to report) - see
    test_real_xray_core_get_users_stats_with_real_traffic below for that.
    """
    client = XrayClient(real_xray_address)
    await _wait_until_ready(client)
    try:
        empty = await client.get_users_stats()
        assert empty == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_xray_core_get_users_stats_with_real_traffic(container_egress):
    """Full loopback proof: a second, genuine Xray-core container acts as a VLESS
    *client* dialing a server this test stands up itself, proxies one real HTTP
    request through it, and confirms get_users_stats() then reports that user's
    real uplink/downlink byte counts and online IP - this is exactly the empirical
    check (run manually once as a throwaway script against the real
    ghcr.io/xtls/xray-core:latest image, confirming the deployed version is 26.3.27)
    that found GetUsersStats returns UNIMPLEMENTED on that version even though the
    vendored proto (pinned to a later tag) declares it, and that the three-RPC
    fallback (GetAllOnlineUsers + GetStatsOnlineIpList + QueryStats) is what that
    specific real version actually supports - now automated instead of only having
    been checked by hand once.

    The one real HTTP request above goes to example.com - needs container_egress
    (see tests/conftest.py) for the same reason as the connectivity_test.py real
    tests: GitHub Actions' ubuntu-latest runners give spawned containers no
    outbound network path at all.

    Deliberately does NOT use the shared real_xray_address fixture above: that
    fixture's server container only publishes its API port at creation time (the
    only port every *other* test in this file needs) - Docker cannot add a port
    mapping to an already-running container, so a VLESS inbound added dynamically
    afterwards (this test's whole point) would never be reachable from the second,
    client-side container. Confirmed as the actual cause of a real, reproducible
    failure (RemoteDisconnected) during this slice's development - initially
    mis-diagnosed as a startup-timing race, until comparing against a working
    throwaway script showed the only real difference was this port publish.
    """
    docker = pytest.importorskip("docker", reason="docker-py not installed")
    requests = pytest.importorskip("requests", reason="requests not installed")
    from docker.errors import DockerException, NotFound  # noqa: PLC0415

    try:
        dc = docker.from_env()
        dc.ping()
    except DockerException as err:
        pytest.skip(f"no real Docker Engine available in this environment: {err}")

    server_api_port = 21502
    server_vless_port = 21503
    server_container_name = "dist-vpn-agent-test-xray-core-traffic-server"
    user_uuid = "c942492e-7435-5e64-be5f-9deb59c41922"
    user_email = "stats-traffic-verify@example.com"
    client_container_name = "dist-vpn-agent-test-xray-core-traffic-client"
    client_socks_port = 21504

    for name in (server_container_name, client_container_name):
        try:
            dc.containers.get(name).remove(force=True)
        except NotFound:
            pass

    server_config_dir = None
    server_container = None
    try:
        # A directory, not tempfile.mkstemp()'s pre-created empty file -
        # ensure_base_config() deliberately no-ops on a path that already exists
        # (see its own idempotency test), so a pre-created empty file would leave
        # an empty, invalid config mounted into the container - exactly what
        # happened here once before this was fixed.
        server_config_dir = tempfile.mkdtemp()
        server_config_path = pathlib.Path(server_config_dir) / "xray-config.json"
        settings = AgentSettings(
            insecure_dev_mode=True,
            xray_api_address=f"127.0.0.1:{server_api_port}",
        ).model_copy(update={"xray_config_host_path": server_config_path})
        ensure_base_config(settings)

        server_container = dc.containers.run(
            "ghcr.io/xtls/xray-core:latest",
            name=server_container_name,
            command=["run", "-config", "/etc/xray/config.json"],
            volumes={str(server_config_path): {"bind": "/etc/xray/config.json", "mode": "ro"}},
            # Both ports published at creation time - the whole point of this test
            # not reusing the shared fixture, see the docstring above.
            ports={
                f"{server_api_port}/tcp": ("127.0.0.1", server_api_port),
                f"{server_vless_port}/tcp": ("127.0.0.1", server_vless_port),
            },
            detach=True,
        )

        client = XrayClient(f"127.0.0.1:{server_api_port}")
        await _wait_until_ready(client)

        await client.add_inbound(
            json.dumps(
                {
                    "tag": "traffic-verify-in",
                    "listen": "0.0.0.0",
                    "port": server_vless_port,
                    "protocol": "vless",
                    "settings": {"users": [], "decryption": "none"},
                    "streamSettings": {"network": "raw", "security": "none"},
                }
            )
        )
        await client.add_user("traffic-verify-in", uuid=user_uuid, email=user_email, flow="")
        await client.add_routing_rule(
            "traffic-verify-rule", json.dumps({"inboundTag": ["traffic-verify-in"], "tag": "direct"})
        )

        client_config_path = None
        try:
            client_xray_config = {
                "log": {"loglevel": "warning"},
                "inbounds": [{"listen": "0.0.0.0", "port": client_socks_port, "protocol": "socks", "settings": {"udp": False}}],
                "outbounds": [
                    {
                        "tag": "srv",
                        "protocol": "vless",
                        "settings": {
                            "vnext": [
                                {
                                    "address": "host.docker.internal",
                                    "port": server_vless_port,
                                    "users": [{"id": user_uuid, "encryption": "none"}],
                                }
                            ]
                        },
                        "streamSettings": {"network": "raw", "security": "none"},
                    }
                ],
            }
            fd, client_config_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(client_xray_config, f)

            client_container = dc.containers.run(
                "ghcr.io/xtls/xray-core:latest",
                name=client_container_name,
                command=["run", "-config", "/etc/xray/config.json"],
                volumes={client_config_path: {"bind": "/etc/xray/config.json", "mode": "ro"}},
                ports={f"{client_socks_port}/tcp": ("127.0.0.1", client_socks_port)},
                extra_hosts={"host.docker.internal": "host-gateway"},
                detach=True,
            )
            try:
                proxies = {
                    "http": f"socks5h://127.0.0.1:{client_socks_port}",
                    "https": f"socks5h://127.0.0.1:{client_socks_port}",
                }
                last_err: Exception | None = None
                for _ in range(20):
                    try:
                        response = requests.get("http://example.com", proxies=proxies, timeout=5)
                        assert response.status_code == 200
                        break
                    except Exception as err:  # noqa: BLE001 - client container needs a moment to bind
                        last_err = err
                        await asyncio.sleep(0.5)
                else:
                    raise AssertionError(f"tunnel never became usable: {last_err}")
            finally:
                client_container.remove(force=True)
        finally:
            if client_config_path:
                os.unlink(client_config_path)

        stats = await client.get_users_stats()
        this_user = next((u for u in stats if u.email == user_email), None)
        assert this_user is not None, f"expected {user_email} in get_users_stats() after real traffic: {stats}"
        assert this_user.uplink_bytes > 0
        assert this_user.downlink_bytes > 0
        assert this_user.online_ips  # real client IP as seen inside the server container's netns

        await client.remove_user("traffic-verify-in", email=user_email)
        await client.remove_inbound("traffic-verify-in")
        await client.close()
    finally:
        if server_container is not None:
            server_container.remove(force=True)
        if server_config_dir:
            shutil.rmtree(server_config_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_real_xray_core_add_remove_outbound(real_xray_address):
    client = XrayClient(real_xray_address)
    await _wait_until_ready(client)
    try:
        await client.add_outbound(json.dumps({"tag": "verify-out", "protocol": "freedom", "settings": {}}))
        await client.remove_outbound("verify-out")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_xray_core_add_remove_routing_rule(real_xray_address):
    """Exercises the router.Config-wrapper fix (see add_routing_rule's
    comment in xray_client.py) against real Router.AddRule -- without it
    this fails with a real Xray error ("AddRule: config type error").
    """
    client = XrayClient(real_xray_address)
    await _wait_until_ready(client)
    try:
        rule_json = json.dumps({"domain": [{"custom": {"type": "Domain", "value": "example.com"}}], "tag": "direct"})
        await client.add_routing_rule("verify-rule", rule_json)
        await client.remove_routing_rule("verify-rule")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_xray_core_override_balancer_surfaces_real_error(real_xray_address):
    """No balancer is configured in the minimal test config, so this is
    expected to fail -- the point is confirming the *real* Xray error
    ("cannot find tag") comes through XrayClientError verbatim, proving the
    wire request itself reached RoutingService.OverrideBalancerTarget and
    was rejected by real Xray logic, not by translation-layer validation
    (which would raise before ever calling the RPC).
    """
    client = XrayClient(real_xray_address)
    await _wait_until_ready(client)
    try:
        with pytest.raises(XrayClientError, match="cannot find tag"):
            await client.override_balancer("nonexistent-balancer", "direct")
    finally:
        await client.close()
