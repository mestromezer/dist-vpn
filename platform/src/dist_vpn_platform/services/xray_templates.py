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


# Shared REALITY camouflage target for both hops - an unmodified, real, popular HTTPS
# site the TLS handshake mimics (TZ-3 §0 "REALITY... dest"). Not yet operator-
# configurable (see platform/README.md TODO); using the same one for both hops is a
# simplifying assumption for this slice, not a correctness requirement - Hop A and Hop B
# have independent key material either way (see security/reality_keys.py) so they don't
# need distinct dests to avoid collisions.
REALITY_DEST = "www.microsoft.com:443"
REALITY_SERVER_NAME = "www.microsoft.com"

# Listen/dial port shared by Hop A (client-facing) and Hop B (backhaul) - matches
# REALITY_DEST's own port and provisioning/group_vars/entry_nodes.yml's firewall rule
# (ufw allows 443/tcp on entry nodes specifically because Hop A listens here). Both
# build_hopa_inbound_json and build_hopb_inbound_json used to omit "port" entirely -
# agent/src/dist_vpn_agent/xray_client.py's _build_inbound_handler_config treats it as
# *optional* (silently produces an inbound with no port_list, i.e. nothing actually
# listening) rather than raising, so this was a second, independent hard blocker beyond
# the empty REALITY keys - found while writing the two-VDS deployment walkthrough.
HOP_LISTEN_PORT = 443


def build_hopa_inbound_json(cascade_id: str, hop_a_preset: str, reality_private_key: str, reality_short_id: str) -> str:
    """Entry-node (Hop A), client-facing inbound. Defaults to VLESS+REALITY on raw TCP
    (P1-equivalent) regardless of the requested preset name, since the actual P1-P4
    template library (TZ-4 §3) is Agent-side and not reproduced here - `hop_a_preset`
    is carried through as metadata (`_presetHint`) for the Agent to interpret if/when
    it implements the real preset selection.

    `reality_private_key`/`reality_short_id` come from security/reality_keys.py, generated
    once per cascade by the caller (api/routes/cascades.py) and persisted on the Cascade
    row - this function is a pure JSON builder, it does not generate or store key material
    itself."""
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
                    "dest": REALITY_DEST,
                    "serverNames": [REALITY_SERVER_NAME],
                    "privateKey": reality_private_key,
                    "shortIds": [reality_short_id],
                },
            },
            "_presetHint": hop_a_preset,
        }
    )


def build_hopb_outbound_json(
    cascade_id: str,
    exit_node_id: str,
    exit_endpoint: str,
    reality_public_key: str,
    reality_short_id: str,
) -> str:
    """Entry-node chained outbound toward one Hop B (exit) candidate. Tag is prefixed
    with hopb_outbound_tag_prefix() so the native balancer's `selector` can pick up
    additions to the candidate pool (TZ-5 §4.2 / TZ-3 §0 "Нативный failover
    balancer+observatory+fallbackTag").

    `reality_public_key`/`reality_short_id` are the *exit* node's Hop B inbound identity
    (build_hopb_inbound_json below) - an outbound acting as a REALITY client only ever
    needs the server's public key, never a private key of its own (asymmetric, unlike
    the inbound side)."""
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
                "network": "raw",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "serverName": REALITY_SERVER_NAME,
                    "fingerprint": "chrome",
                    "publicKey": reality_public_key,
                    "shortId": reality_short_id,
                },
            },
        }
    )


def build_hopb_inbound_json(cascade_id: str, reality_private_key: str, reality_short_id: str) -> str:
    """Exit-node inbound accepting the chained connection from the entry node's Hop B
    outbound above (same transport/security settings on both ends of that hop).
    `reality_private_key`/`reality_short_id` are this exit node's own Hop B REALITY
    identity for this (cascade, exit_node) pairing - see models/cascade.py
    CascadeExitNode.hopb_reality_*."""
    return json.dumps(
        {
            "tag": hopb_inbound_tag(cascade_id),
            "listen": "0.0.0.0",
            "port": HOP_LISTEN_PORT,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "raw",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": REALITY_DEST,
                    "serverNames": [REALITY_SERVER_NAME],
                    "privateKey": reality_private_key,
                    "shortIds": [reality_short_id],
                },
            },
        }
    )


def build_vless_uri(
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
    preset port selection)."""
    port = HOP_LISTEN_PORT
    params = {
        "security": "reality",
        "sni": REALITY_SERVER_NAME,
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
