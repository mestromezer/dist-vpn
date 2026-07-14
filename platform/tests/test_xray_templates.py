"""services/xray_templates.py - REALITY donor selection/rotation and the JSON/URI
builders that depend on it choosing consistently across independent call sites."""
from __future__ import annotations

import json
import urllib.parse

from dist_vpn_platform.services import xray_templates as xray


def test_select_reality_donor_is_deterministic_for_the_same_seed() -> None:
    assert xray._select_reality_donor("cascade-1") == xray._select_reality_donor("cascade-1")


def test_select_reality_donor_spreads_across_the_pool() -> None:
    # Not a proof of uniform distribution - just that different seeds don't all
    # collapse onto the same donor by construction (the bug this would have been the
    # old single hardcoded REALITY_SERVER_NAME, which this replaces).
    seeds = [f"cascade-{i}" for i in range(20)]
    donors = {xray._select_reality_donor(seed) for seed in seeds}
    assert len(donors) > 1


def test_select_reality_donor_respects_an_explicit_pool_override() -> None:
    donor = xray._select_reality_donor("any-seed", pool=["only-option.example"])
    assert donor == "only-option.example"


def test_select_reality_donor_rejects_an_empty_pool() -> None:
    try:
        xray._select_reality_donor("seed", pool=[])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an empty donor pool")


def test_hopa_inbound_dest_and_server_names_agree_on_the_same_donor() -> None:
    inbound = json.loads(xray.build_hopa_inbound_json("cascade-1", "p1", "priv-key", "short-id"))
    reality = inbound["streamSettings"]["realitySettings"]
    donor = reality["serverNames"][0]
    assert reality["dest"] == f"{donor}:443"


def test_hopa_inbound_donor_is_stable_across_calls_for_the_same_cascade() -> None:
    first = json.loads(xray.build_hopa_inbound_json("cascade-1", "p1", "priv-key", "short-id"))
    second = json.loads(xray.build_hopa_inbound_json("cascade-1", "p1", "priv-key", "short-id"))
    assert (
        first["streamSettings"]["realitySettings"]["serverNames"]
        == second["streamSettings"]["realitySettings"]["serverNames"]
    )


def test_hopb_outbound_and_inbound_agree_on_the_same_donor_for_the_same_link() -> None:
    """The actual correctness property that matters: if these two ever pick different
    donors for the same (cascade_id, exit_node_id) pair, the Hop B handshake fails - the
    client-role outbound's `serverName` must match what the server-role inbound accepts
    in `serverNames`."""
    outbound = json.loads(
        xray.build_hopb_outbound_json("cascade-1", "exit-1", "203.0.113.5", "pub-key", "short-id")
    )
    inbound = json.loads(xray.build_hopb_inbound_json("cascade-1", "exit-1", "priv-key", "short-id"))

    outbound_donor = outbound["streamSettings"]["realitySettings"]["serverName"]
    inbound_donor = inbound["streamSettings"]["realitySettings"]["serverNames"][0]
    assert outbound_donor == inbound_donor


def test_hopb_inbound_donor_is_keyed_on_both_cascade_and_exit_node() -> None:
    """The seed is f"{cascade_id}:{exit_node_id}", not cascade_id alone - otherwise every
    exit candidate on the same cascade would share one donor, defeating the point of
    per-link selection. Verified directly against _select_reality_donor rather than by
    hoping two arbitrary exit_node_id values happen to hash to different pool entries
    (a 4-entry pool makes that flaky)."""
    inbound = json.loads(xray.build_hopb_inbound_json("cascade-1", "exit-a", "priv-key", "short-id"))
    donor = inbound["streamSettings"]["realitySettings"]["serverNames"][0]
    assert donor == xray._select_reality_donor("cascade-1:exit-a")


def test_p4_ws_path_is_deterministic_and_cascade_specific() -> None:
    assert xray.p4_ws_path("cascade-1") == xray.p4_ws_path("cascade-1")
    assert xray.p4_ws_path("cascade-1") != xray.p4_ws_path("cascade-2")
    assert xray.p4_ws_path("cascade-1").startswith("/")


def test_hopa_p4_inbound_uses_ws_tls_not_reality() -> None:
    inbound = json.loads(xray.build_hopa_p4_inbound_json("cascade-1"))
    stream = inbound["streamSettings"]
    assert stream["network"] == "ws"
    assert stream["security"] == "tls"
    assert "realitySettings" not in stream
    assert stream["wsSettings"]["path"] == xray.p4_ws_path("cascade-1")
    assert inbound["port"] == xray.P4_LISTEN_PORT
    assert inbound["port"] != xray.HOP_LISTEN_PORT  # must not collide with the REALITY inbound
    cert = stream["tlsSettings"]["certificates"][0]
    assert cert["certificateFile"] == xray.P4_TLS_CERT_PATH
    assert cert["keyFile"] == xray.P4_TLS_KEY_PATH


def test_hopa_p4_inbound_tag_is_distinct_from_the_reality_inbound_tag() -> None:
    p4_inbound = json.loads(xray.build_hopa_p4_inbound_json("cascade-1"))
    reality_inbound = json.loads(xray.build_hopa_inbound_json("cascade-1", "p1", "priv-key", "short-id"))
    assert p4_inbound["tag"] != reality_inbound["tag"]


def test_build_vless_uri_p4_uses_the_p4_domain_and_ws_path() -> None:
    uri = xray.build_vless_uri_p4(
        cascade_id="cascade-1",
        user_uuid="11111111-1111-1111-1111-111111111111",
        p4_domain="cdn.example.com",
        label="test-user",
    )
    assert uri.startswith("vless://11111111-1111-1111-1111-111111111111@cdn.example.com:8443?")
    assert "security=tls" in uri
    assert "type=ws" in uri
    assert "sni=cdn.example.com" in uri
    assert urllib.parse.quote(xray.p4_ws_path("cascade-1"), safe="") in uri


def test_build_client_routing_rules_direct_routes_local_resources() -> None:
    rules = xray.build_client_routing_rules()
    direct_rules = [r for r in rules if r.get("outboundTag") == "direct"]
    assert any(r.get("domain") == ["geosite:category-ru"] for r in direct_rules)
    assert any(r.get("ip") == ["geoip:ru"] for r in direct_rules)


def test_build_client_routing_rules_blocks_known_ip_leak_domains() -> None:
    rules = xray.build_client_routing_rules()
    block_rules = [r for r in rules if r.get("outboundTag") == "block"]
    assert len(block_rules) == 1
    blocked_domains = block_rules[0]["domain"]
    assert "domain:ipify.org" in blocked_domains
    assert "domain:icanhazip.com" in blocked_domains
    # Every entry uses the "domain:" prefix (matches the domain + subdomains) - not a
    # bare hostname, which Xray would treat as a full/exact match only.
    assert all(d.startswith("domain:") for d in blocked_domains)


def test_build_client_routing_rules_never_combines_domain_and_ip_in_one_rule() -> None:
    # Xray ANDs every field present on a single RuleObject - a rule with both `domain`
    # and `ip` would require both to match the same connection, not either.
    for rule in xray.build_client_routing_rules():
        assert not ("domain" in rule and "ip" in rule)


def test_build_vless_uri_sni_matches_the_hopa_inbound_donor_for_the_same_cascade() -> None:
    inbound = json.loads(xray.build_hopa_inbound_json("cascade-1", "p1", "priv-key", "short-id"))
    inbound_donor = inbound["streamSettings"]["realitySettings"]["serverNames"][0]

    uri = xray.build_vless_uri(
        cascade_id="cascade-1",
        user_uuid="11111111-1111-1111-1111-111111111111",
        entry_public_address="203.0.113.10",
        hopa_reality_public_key="pub-key",
        hopa_reality_short_id="short-id",
        label="test-user",
    )
    assert f"sni={inbound_donor}" in uri
