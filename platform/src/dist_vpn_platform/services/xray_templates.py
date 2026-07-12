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


def build_hopa_inbound_json(cascade_id: str, hop_a_preset: str) -> str:
    """Entry-node (Hop A), client-facing inbound. Defaults to VLESS+REALITY on raw TCP
    (P1-equivalent) regardless of the requested preset name, since the actual P1-P4
    template library (TZ-4 §3) is Agent-side and not reproduced here - `hop_a_preset`
    is carried through as metadata (`_presetHint`) for the Agent to interpret if/when
    it implements the real preset selection."""
    return json.dumps(
        {
            "tag": hopa_inbound_tag(cascade_id),
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "raw",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": "www.example.com:443",
                    "serverNames": ["www.example.com"],
                    # TODO: real key material must come from a provisioning step (not
                    # yet wired - see platform/README.md TODO on step-ca/mTLS), never
                    # hardcoded in Platform.
                    "privateKey": "",
                    "shortIds": [""],
                },
            },
            "_presetHint": hop_a_preset,
        }
    )


def build_hopb_outbound_json(cascade_id: str, exit_node_id: str, exit_endpoint: str) -> str:
    """Entry-node chained outbound toward one Hop B (exit) candidate. Tag is prefixed
    with hopb_outbound_tag_prefix() so the native balancer's `selector` can pick up
    additions to the candidate pool (TZ-5 §4.2 / TZ-3 §0 "Нативный failover
    balancer+observatory+fallbackTag")."""
    return json.dumps(
        {
            "tag": hopb_outbound_tag(cascade_id, exit_node_id),
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": exit_endpoint,
                        "port": 443,
                        "users": [{"id": "", "encryption": "none", "flow": ""}],
                    }
                ]
            },
            "streamSettings": {"network": "raw", "security": "reality", "realitySettings": {}},
        }
    )


def build_hopb_inbound_json(cascade_id: str) -> str:
    """Exit-node inbound accepting the chained connection from the entry node's Hop B
    outbound above (same transport/security settings on both ends of that hop)."""
    return json.dumps(
        {
            "tag": hopb_inbound_tag(cascade_id),
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {"network": "raw", "security": "reality", "realitySettings": {}},
        }
    )


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
