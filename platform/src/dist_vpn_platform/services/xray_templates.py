"""Builds the Xray-core JSON fragments carried inside `Command.add_inbound.inbound_json`
/ `add_outbound.outbound_json` / `add_routing_rule.rule_json` (contracts/proto/agent_control.proto).

IMPORTANT - provenance/assumption note (see platform/README.md "Отклонения от ТЗ"):
docs/TZ-3-arhitektura-flota.md §0 says the concrete P1-P4 preset templates and the
exact `balancer`/`observatory`/`fallbackTag` JSON were "проверены ранее в проекте" but
were **physically lost** in a disk failure before this repository existed - only the
textual description of the fields survived (see TZ-3 §0 table: "VLESS+REALITY, схема
полей (`realitySettings`, `network:"raw"/"xhttp"/"grpc"`, `password`=бывший
`publicKey`...)" and TZ-5 §4.2: "балансировщик с префиксным `selector`, `observatory`,
`fallbackTag`"). There is no literal JSON shape anywhere in this monorepo to copy
(checked `agent/`, `provisioning/`, `contracts/` - all either empty scaffolding or
textual-only). What follows is a best-effort reconstruction of the field *shape*
using standard Xray-core config conventions consistent with that textual description,
NOT a byte-for-byte copy of a previously-verified config. Treat the concrete values
(`dest`, ports, `strategy`) as placeholders - the real preset library (P1-P4) is
Agent-side work (TZ-4 §3) out of scope for platform/.

REALITY key material (`privateKey`/`publicKey`/`shortIds`) is real, not a placeholder:
generated per-cascade/per-(cascade,exit_node) by security/reality_keys.py and threaded
through as parameters below - this used to be hardcoded `""` (a hard blocker, REALITY
cannot complete a handshake with an empty private key), fixed once the encoding was
confirmed against Xray-core's actual `x25519` CLI source (see reality_keys.py).

Tag naming scheme (our own invention, needs to match whatever api/routes/cascades.py
and api/routes/users.py send - see hopa_inbound_tag() etc. below, used consistently by
both the command builders here and the AddUser/RemoveUser dispatch in
api/routes/users.py):
  hopa-in-<cascade_id>            Hop A (client-facing) inbound on the entry node
  hopb-out-<cascade_id>-<exit_id> Hop A -> Hop B chained outbound, one per exit candidate
  hopb-in-<cascade_id>            Hop B (backhaul-facing) inbound on an exit node
  balancer-<cascade_id>           balancer tag referenced by the routing rule
  rule-<cascade_id>                routing rule tag
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse


def hopa_inbound_tag(cascade_id: str) -> str:
    return f"hopa-in-{cascade_id}"


def hopb_outbound_tag(cascade_id: str, exit_node_id: str) -> str:
    return f"hopb-out-{cascade_id}-{exit_node_id}"


def hopb_outbound_tag_prefix(cascade_id: str) -> str:
    """Prefix shared by every Hop B outbound of this cascade - this is what the
    balancer's `selector` matches against (TZ-5 §4.2: "балансировщик с префиксным
    selector"), so a newly-added candidate outbound is picked up without touching the
    routing rule itself (see the open question logged in TZ-3 §0, line ~95, about
    whether that auto-pickup is confirmed - flagged, not silently assumed true)."""
    return f"hopb-out-{cascade_id}-"


def hopb_inbound_tag(cascade_id: str) -> str:
    return f"hopb-in-{cascade_id}"


def balancer_tag(cascade_id: str) -> str:
    return f"balancer-{cascade_id}"


def routing_rule_tag(cascade_id: str) -> str:
    return f"rule-{cascade_id}"


# Pool of REALITY camouflage donor domains (TZ-3 §0 "REALITY... dest"/"serverNames").
# A single hardcoded donor (this constant used to be one bare string, REALITY_SERVER_NAME
# = "www.microsoft.com", used unconditionally everywhere) is itself a DPI fingerprint risk
# independent of REALITY's own protocol-level camouflage: a given entry/exit IP always
# presenting the exact same SNI to every client, forever, is a detectable signature (see
# platform/README.md "Отклонения от ТЗ" for the write-up this closes). Selecting a
# different donor per cascade (Hop A) and per (cascade, exit_node) link (Hop B) - via
# _select_reality_donor's stable hash of the id(s), not randomly on every call - spreads
# that signature across the pool below instead of collapsing the whole fleet onto one
# domain, while still being deterministic (the same cascade/link always regenerates the
# same donor on redeploy/AddInbound retry, rather than drifting).
#
# This default pool is a REASONABLE STARTING POINT, not a verified-good list for any
# particular deployment: whether a given domain is actually a safe REALITY donor depends
# on live, current facts (CDN/edge presence that would make "a local user reaching it
# look anomalous" from inside the target region, whether it still serves TLS 1.3+HTTP/2,
# whether it redirects) that only a tool like RealiTLScanner run against the real
# entry/exit VPS can confirm at deploy time. Re-verify (or override this list) before
# relying on it in production - see platform/README.md.
REALITY_DONOR_POOL: list[str] = [
    "www.microsoft.com",
    "www.swift.com",
    "www.cisco.com",
    "download.windowsupdate.com",
]


def _select_reality_donor(seed: str, pool: list[str] | None = None) -> str:
    """Deterministic pick from REALITY_DONOR_POOL (or an explicit override), keyed on
    `seed` - a cascade id for Hop A, or f"{cascade_id}:{exit_node_id}" for a Hop B link -
    so the same cascade/link always regenerates the same donor rather than silently
    drifting to a different SNI on every AddInbound call. Not cryptographically
    meaningful (this is a fleet-diversity spread, not a secret), so a plain stable hash
    is enough - no need for the CSPRNG security/reality_keys.py uses for actual key
    material."""
    candidates = pool if pool is not None else REALITY_DONOR_POOL
    if not candidates:
        raise ValueError("REALITY donor pool is empty")
    index = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(candidates)
    return candidates[index]


# Listen/dial port shared by Hop A (client-facing) and Hop B (backhaul) - matches
# REALITY_DEST's own port and provisioning/group_vars/entry_nodes.yml's firewall rule
# (ufw allows 443/tcp on entry nodes specifically because Hop A listens here). Both
# build_hopa_inbound_json and build_hopb_inbound_json used to omit "port" entirely -
# agent/src/dist_vpn_agent/xray_client.py's _build_inbound_handler_config treats it as
# *optional* (silently produces an inbound with no port_list, i.e. nothing actually
# listening) rather than raising, so this was a second, independent hard blocker beyond
# the empty REALITY keys - found while writing the two-VDS deployment walkthrough.
HOP_LISTEN_PORT = 443

# P4 preset (TZ-4 §3.3/§3.4: "VLESS/TLS на WebSocket") - a fallback for when a client
# can't reach the entry node's raw REALITY port/IP directly (e.g. a network that only
# allows traffic to addresses it recognizes as ordinary HTTPS via an already-trusted
# CDN). Separate port from HOP_LISTEN_PORT: it's a second, simultaneous inbound on the
# same entry node, not a replacement, and Xray can't bind two inbounds to the same port.
# 8443 is one of Cloudflare's supported proxied-HTTPS origin ports, so a CDN can be
# layered in front later without changing this - but P4 does not require CDN fronting
# to function; it works as a plain HTTPS-fronted VLESS+WS+TLS endpoint on its own.
P4_LISTEN_PORT = 8443

# Fixed on-disk paths (inside the Agent container) for the P4 TLS certificate/key -
# same "fixed convention path, not a Platform-configurable one" pattern as the mTLS CA
# cert path in provisioning/roles/agent_install. Platform never sees the private key:
# it only needs to know where the Agent already has it, to put the path in the
# generated Xray config JSON (a path is not a secret; the file's contents are).
#
# IMPORTANT - deliberately manual for this iteration, not automated: getting a real
# cert onto this path (via certbot, a Cloudflare Origin Certificate, etc.) is an
# operator step, not something this code does. TZ-4 §3.4 leaves the door open to a
# future ACME client managed by the Agent itself; that's not implemented here - see
# platform/README.md "Отклонения от ТЗ".
P4_TLS_CERT_PATH = "/etc/dist-vpn-agent/tls/p4-cert.pem"
P4_TLS_KEY_PATH = "/etc/dist-vpn-agent/tls/p4-key.pem"


def p4_ws_path(cascade_id: str) -> str:
    """WebSocket path for a cascade's P4 inbound - derived, not persisted (same
    "compute from the id instead of adding a column" choice as REALITY donor
    selection). Not a security boundary (WS path guessing isn't the threat model here -
    the TLS layer and VLESS UUID are), just avoids every cascade sharing one obvious,
    literal `/ws` path."""
    digest = hashlib.sha256(cascade_id.encode("utf-8")).hexdigest()[:12]
    return f"/ws-{digest}"


def build_hopa_p4_inbound_json(cascade_id: str) -> str:
    """Entry-node P4 inbound: VLESS over WebSocket, terminated with a real TLS
    certificate (P4_TLS_CERT_PATH/P4_TLS_KEY_PATH) rather than REALITY's borrowed
    handshake - see this module's P4 constants for the port/cert-path rationale.
    Unlike build_hopa_inbound_json, there's no REALITY key material to thread through
    (plain TLS has no equivalent of a REALITY private key/short id)."""
    return json.dumps(
        {
            "tag": f"{hopa_inbound_tag(cascade_id)}-p4",
            "listen": "0.0.0.0",
            "port": P4_LISTEN_PORT,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "ws",
                "security": "tls",
                "tlsSettings": {
                    "certificates": [{"certificateFile": P4_TLS_CERT_PATH, "keyFile": P4_TLS_KEY_PATH}],
                    "alpn": ["http/1.1"],
                },
                "wsSettings": {"path": p4_ws_path(cascade_id)},
            },
            "_presetHint": "p4",
        }
    )


def build_vless_uri_p4(cascade_id: str, user_uuid: str, p4_domain: str, label: str) -> str:
    """Composes the `vless://` share-link for the P4 inbound above - the WS+TLS
    counterpart to build_vless_uri's REALITY URI. No REALITY-specific query params
    (pbk/sid/fp's REALITY meaning) since P4 is plain TLS; `sni`/`host` both need to be
    the real domain the TLS certificate at P4_TLS_CERT_PATH actually covers."""
    params = {
        "security": "tls",
        "sni": p4_domain,
        "host": p4_domain,
        "type": "ws",
        # Not pre-quoted: urlencode() below already percent-encodes every value -
        # quoting here too would double-encode it (the "/" in the path would come out
        # as "%2F" and *then* the "%" of that would be re-encoded to "%25").
        "path": p4_ws_path(cascade_id),
        "flow": "",
    }
    query = urllib.parse.urlencode(params)
    fragment = urllib.parse.quote(f"{label}-p4")
    return f"vless://{user_uuid}@{p4_domain}:{P4_LISTEN_PORT}?{query}#{fragment}"


def build_hopa_inbound_json(cascade_id: str, hop_a_preset: str, reality_private_key: str, reality_short_id: str) -> str:
    """Entry-node (Hop A), client-facing inbound. Defaults to VLESS+REALITY on raw TCP
    (P1-equivalent) regardless of the requested preset name, since the actual P1-P4
    template library (TZ-4 §3) is Agent-side and not reproduced here - `hop_a_preset`
    is carried through as metadata (`_presetHint`) for the Agent to interpret if/when
    it implements the real preset selection. The "p4" preset specifically is now handled
    by build_hopa_p4_inbound_json above instead - see create_cascade in
    api/routes/cascades.py, which calls this function for every *other* requested preset
    and build_hopa_p4_inbound_json separately when "p4" is one of them.

    `reality_private_key`/`reality_short_id` come from security/reality_keys.py, generated
    once per cascade by the caller (api/routes/cascades.py) and persisted on the Cascade
    row - this function is a pure JSON builder, it does not generate or store key material
    itself.

    REALITY donor is picked deterministically from REALITY_DONOR_POOL, keyed on
    `cascade_id` alone - see _select_reality_donor and the pool's own docstring for why
    this isn't just the old single hardcoded www.microsoft.com anymore. build_vless_uri
    below derives the *same* donor independently (same seed = cascade_id), so the
    client-facing SNI always matches what this inbound actually expects without needing
    to thread the chosen donor through as its own persisted field."""
    donor = _select_reality_donor(cascade_id)
    return json.dumps(
        {
            "tag": hopa_inbound_tag(cascade_id),
            "listen": "0.0.0.0",
            "port": HOP_LISTEN_PORT,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "raw",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": f"{donor}:443",
                    "serverNames": [donor],
                    "privateKey": reality_private_key,
                    "shortIds": [reality_short_id],
                },
            },
            "_presetHint": hop_a_preset,
        }
    )


# Hop B preset rotation (services/link_negotiation.py, slice 12, iterates through
# these in order on a failed connectivity test). P1 (raw REALITY) is the default/
# first-tried preset, matching every pre-slice-11 cascade's implicit behavior - a
# cascade created before this slice, or with no preset negotiation ever run, still
# behaves exactly as before. P4 (real-TLS-cert backhaul, mirroring Hop A's
# build_hopa_p4_inbound_json) is deliberately NOT included here yet - it would need an
# exit-node equivalent of Node.p4_domain, a materially bigger schema question than
# reusing the transport variety P2/P3 already add; deprioritized until P1-P3 alone are
# shown insufficient (see platform/README.md).
HOPB_PRESET_ROTATION: list[str] = ["p1", "p2", "p3"]


def xhttp_path(cascade_id: str, exit_node_id: str) -> str:
    """Derived, not persisted - same pattern as p4_ws_path (avoids every Hop B xhttp
    link sharing one obvious literal path; not a security boundary, REALITY/the VLESS
    UUID are)."""
    digest = hashlib.sha256(f"{cascade_id}:{exit_node_id}".encode("utf-8")).hexdigest()[:12]
    return f"/x-{digest}"


def grpc_service_name(cascade_id: str, exit_node_id: str) -> str:
    """Derived, not persisted - same rationale as xhttp_path above. gRPC's
    serviceName must be a valid gRPC service-name-shaped path segment (no leading
    slash, unlike xhttp's path) - see transport/internet/grpc/config.proto's
    `service_name` field, vendored at agent/vendor/xray-proto/transport/internet/grpc/
    config.proto."""
    digest = hashlib.sha256(f"{cascade_id}:{exit_node_id}".encode("utf-8")).hexdigest()[:12]
    return f"g-{digest}"


def _hopb_stream_settings(preset: str, cascade_id: str, exit_node_id: str) -> dict:
    """The `network`/`<network>Settings` portion of Hop B's streamSettings, shared by
    build_hopb_outbound_json/build_hopb_inbound_json below - only the transport varies
    by preset, `security: "reality"`/`realitySettings` stay identical in shape on both
    functions regardless of preset (only the REALITY key material itself, which the
    callers already handle, differs).

    Field names/registered transport names verified against the vendored Xray-core
    proto (agent/vendor/xray-proto/transport/internet/{splithttp,grpc}/config.proto,
    tag v26.7.11) and agent/src/dist_vpn_agent/xray_client.py's own
    `_TRANSPORT_SETTINGS` mapping, not guessed - `xhttpSettings.path`/`.mode` are
    already lowerCamelCase in the proto's own field names (no JSON-casing transform
    involved); `grpcSettings.serviceName` is the JSON-camelCase form of the proto's
    `service_name` (confirmed via protobuf's standard snake_case->camelCase JSON
    mapping, the same one `agent/src/dist_vpn_agent/xray_client.py::_parse_settings`
    relies on via `google.protobuf.json_format`).
    """
    if preset == "p1":
        return {"network": "raw"}
    if preset == "p2":
        donor = _select_reality_donor(f"{cascade_id}:{exit_node_id}")
        return {
            "network": "xhttp",
            "xhttpSettings": {"path": xhttp_path(cascade_id, exit_node_id), "mode": "auto", "host": donor},
        }
    if preset == "p3":
        return {
            "network": "grpc",
            "grpcSettings": {"serviceName": grpc_service_name(cascade_id, exit_node_id)},
        }
    raise ValueError(f"unsupported Hop B preset: {preset!r} (supported: {HOPB_PRESET_ROTATION})")


def build_hopb_outbound_json(
    cascade_id: str,
    exit_node_id: str,
    exit_endpoint: str,
    preset: str,
    reality_public_key: str,
    reality_short_id: str,
) -> str:
    """Entry-node chained outbound toward one Hop B (exit) candidate. Tag is prefixed
    with hopb_outbound_tag_prefix() so the native balancer's `selector` can pick up
    additions to the candidate pool (TZ-5 §4.2 / TZ-3 §0 "Нативный failover
    balancer+observatory+fallbackTag").

    `preset` (new, slice 11) selects the transport envelope - see
    _hopb_stream_settings and HOPB_PRESET_ROTATION above. Callers (services/
    link_negotiation.py) must dispatch a matching RemoveOutbound+AddOutbound pair when
    switching a link's preset - Xray has no in-place "change an existing outbound's
    transport" operation, only add/remove.

    `reality_public_key`/`reality_short_id` are the *exit* node's Hop B inbound identity
    (build_hopb_inbound_json below) - an outbound acting as a REALITY client only ever
    needs the server's public key, never a private key of its own (asymmetric, unlike
    the inbound side).

    REALITY donor is picked deterministically from REALITY_DONOR_POOL, keyed on
    f"{cascade_id}:{exit_node_id}" - build_hopb_inbound_json derives the same donor for
    the same (cascade_id, exit_node_id) pair, since `serverName` here must match what
    that inbound's own `serverNames` accepts for the handshake to complete. Deliberately
    a different seed than Hop A's (cascade_id alone) so a cascade's Hop A and each of its
    Hop B links don't all collapse onto the same donor by construction."""
    donor = _select_reality_donor(f"{cascade_id}:{exit_node_id}")
    return json.dumps(
        {
            "tag": hopb_outbound_tag(cascade_id, exit_node_id),
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": exit_endpoint,
                        "port": HOP_LISTEN_PORT,
                        "users": [{"id": "", "encryption": "none", "flow": ""}],
                    }
                ]
            },
            "streamSettings": {
                **_hopb_stream_settings(preset, cascade_id, exit_node_id),
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "serverName": donor,
                    "fingerprint": "chrome",
                    "publicKey": reality_public_key,
                    "shortId": reality_short_id,
                },
            },
            "_presetHint": preset,
        }
    )


def build_hopb_inbound_json(
    cascade_id: str, exit_node_id: str, preset: str, reality_private_key: str, reality_short_id: str
) -> str:
    """Exit-node inbound accepting the chained connection from the entry node's Hop B
    outbound above (same transport/security settings on both ends of that hop, `preset`
    must match what build_hopb_outbound_json was called with for this same link - see
    that function's docstring). `reality_private_key`/`reality_short_id` are this exit
    node's own Hop B REALITY identity for this (cascade, exit_node) pairing - see
    models/exit_link_allocation.py's ExitLinkAllocation.hopb_reality_*.

    `exit_node_id` is needed only to derive the same REALITY donor/xhttp-path/grpc-
    service-name build_hopb_outbound_json picked for this (cascade_id, exit_node_id)
    pair - see that function's docstring. Not persisted anywhere new; the caller
    already has both ids."""
    donor = _select_reality_donor(f"{cascade_id}:{exit_node_id}")
    return json.dumps(
        {
            "tag": hopb_inbound_tag(cascade_id),
            "listen": "0.0.0.0",
            "port": HOP_LISTEN_PORT,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                **_hopb_stream_settings(preset, cascade_id, exit_node_id),
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": f"{donor}:443",
                    "serverNames": [donor],
                    "privateKey": reality_private_key,
                    "shortIds": [reality_short_id],
                },
            },
        }
    )


def build_vless_uri(
    cascade_id: str,
    user_uuid: str,
    entry_public_address: str,
    hopa_reality_public_key: str,
    hopa_reality_short_id: str,
    label: str,
) -> str:
    """Composes the `vless://` share-link a real client (v2rayN/NekoBox/sing-box/...)
    can import directly, for the Hop A (client-facing) inbound built above. Query
    parameter names/values follow the community VLESS share-link convention (confirmed
    against github.com/XTLS/Xray-core/discussions/716 this slice) - notably `type=tcp`
    for what the JSON config itself calls `network: "raw"` (the URI convention predates
    and never followed Xray-core's internal tcp->raw rename), and `flow` is left empty
    to match AddUser.flow in api/routes/users.py (no confirmed preset->flow mapping
    survived the TZ-4 §3 loss this project inherited - see that file's docstring).

    Port matches HOP_LISTEN_PORT above, i.e. the Hop A inbound's actual listen port
    (not independently configurable in this slice - see platform/README.md TODO on
    preset port selection).

    `cascade_id` (new) - purely to re-derive the same REALITY donor
    build_hopa_inbound_json picked for this cascade (see _select_reality_donor); the SNI
    a client presents must match what the actual inbound's `serverNames` accepts."""
    port = HOP_LISTEN_PORT
    donor = _select_reality_donor(cascade_id)
    params = {
        "security": "reality",
        "sni": donor,
        "fp": "chrome",
        "pbk": hopa_reality_public_key,
        "sid": hopa_reality_short_id,
        "type": "tcp",
        "flow": "",
    }
    query = urllib.parse.urlencode(params)
    fragment = urllib.parse.quote(label)
    return f"vless://{user_uuid}@{entry_public_address}:{port}?{query}#{fragment}"


def build_balancer_routing_rule_json(cascade_id: str) -> str:
    """Balancer (prefix `selector` over this cascade's Hop B outbounds, `observatory`-
    driven `leastPing` strategy, empty `fallbackTag` = no override) + the routing rule
    binding the Hop A inbound to that balancer (TZ-5 §4.2, TZ-3 §0). Bundled into a
    single AddRoutingRule.rule_json since agent_control.proto has no separate
    "add balancer" op - see the module docstring's provenance note."""
    return json.dumps(
        {
            "balancer": {
                "tag": balancer_tag(cascade_id),
                "selector": [hopb_outbound_tag_prefix(cascade_id)],
                "strategy": {"type": "leastPing"},
                "fallbackTag": "",
            },
            "rule": {
                "type": "field",
                "inboundTag": [hopa_inbound_tag(cascade_id)],
                "balancerTag": balancer_tag(cascade_id),
            },
        }
    )


# Known "what's my IP" / IP-echo services. If a client tunnels everything through the
# cascade *except* local resources (see build_client_routing_rules below), reaching one
# of these through the tunnel would hand the exit node's real IP straight back to a
# third party outside this system - not a hypothetical, this is a real fingerprinting
# vector network operators are known to use against proxy/VPN traffic. Sourced from
# public community write-ups on exactly this failure mode, not exhaustive - operators
# should extend this list rather than treat it as complete.
IP_LEAK_CHECK_DOMAINS: list[str] = [
    "ipify.org",
    "ifconfig.me",
    "ifconfig.co",
    "icanhazip.com",
    "ipinfo.io",
    "whatismyip.com",
    "whatismyipaddress.com",
    "api.myip.com",
    "myip.wtf",
    "2ip.io",
    "2ip.ru",
    "2ip.co",
    "2ip.net",
]


def build_client_routing_rules() -> list[dict]:
    """Xray-compatible routing rule objects (RoutingObject.rules[] shape - same "type":
    "field" convention as build_balancer_routing_rule_json above) meant to be embedded
    into a *client's own local config* (Hiddify/sing-box/v2rayNG etc.), not dispatched
    to an Agent - this is why the result is plain data, not a JSON string like every
    other build_*_json function in this module: the caller (api/routes/users.py
    get_user_connection) hands it back as part of a REST response, where the client
    decides how to fold it into its own config format.

    Two independent concerns, three rules:
      - geosite:category-ru / geoip:ru -> "direct": local resources shouldn't pay the
        extra hop through the cascade, and this is the split-routing/bypass behavior
        TZ never asked the *server* to implement but costs nothing to hand the client
        pre-built rather than leaving every user to assemble it by hand.
      - IP_LEAK_CHECK_DOMAINS -> "block": see that constant's own docstring.
    domain/ip are deliberately separate rule objects rather than combined into one -
    Xray's RuleObject ANDs every field present on a single rule together, so a combined
    {"domain": [...], "ip": [...]} rule would require BOTH to match the same
    connection, not either."""
    return [
        {"type": "field", "domain": ["geosite:category-ru"], "outboundTag": "direct"},
        {"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"},
        {
            "type": "field",
            "domain": [f"domain:{domain}" for domain in IP_LEAK_CHECK_DOMAINS],
            "outboundTag": "block",
        },
    ]
