"""Cascade - an entry node bound to a pool of candidate exit nodes (TZ-5 §3, §4.2).

`status` is stored as an explicit column (not derived on the fly from scattered
counters) per TZ-5 §4.3's explicit requirement: "Хранить состояние каждой связки...
как явную сущность с текущим статусом".
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow
from dist_vpn_platform.models.remote_exit_candidate import RemoteExitCandidate


class CascadeStatus(str, enum.Enum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class Cascade(Base):
    __tablename__ = "cascades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    entry_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.id"), nullable=False)
    status: Mapped[CascadeStatus] = mapped_column(
        sa_enum(CascadeStatus), nullable=False, default=CascadeStatus.DOWN
    )
    # p1..p4 preset identifiers (see history referenced in TZ-5 §4.2) - a short list of
    # strings, portable as JSON across Postgres and SQLite.
    hop_a_presets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Hop A (client-facing) REALITY identity - generated once at cascade creation
    # (services/reality_keys.py), embedded in the AddInbound dispatched to the entry
    # node (services/xray_templates.py build_hopa_inbound_json). The private key never
    # leaves this row/that dispatch - only hopa_reality_public_key/short_id are ever
    # returned by the API (schemas/cascades.py CascadeOut), for composing a client
    # vless:// URI (api/routes/users.py connection endpoint).
    hopa_reality_private_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hopa_reality_public_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hopa_reality_short_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    exit_links: Mapped[list["CascadeExitLink"]] = relationship(
        back_populates="cascade", cascade="all, delete-orphan"
    )

    @property
    def exit_node_ids(self) -> list[str]:
        """Backward-compatible view for callers that only need "which node" (e.g. the
        REST CascadeOut.exit_node_ids field, api/routes/nodes.py::decommission_node).
        Resolves through the link's RemoteExitCandidate - see that model's docstring
        for the local-vs-real-peer remote_node_id distinction. Callers that also need
        per-link status/preset should use `exit_links` directly instead (see
        schemas/cascades.py CascadeExitLinkOut)."""
        return [link.remote_exit_candidate.remote_node_id for link in self.exit_links]


class CascadeExitLinkStatus(str, enum.Enum):
    PENDING = "pending"
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class CascadeExitLink(Base):
    """Association: which exit nodes (via their RemoteExitCandidate row - see that
    model's docstring for why this is not a direct FK to `nodes` anymore) are
    candidates for a given cascade (the "Hop B candidate pool" from TZ-5 §4.2 - Xray's
    own balancer selects among the currently-UP ones natively, see
    services/xray_templates.py build_balancer_routing_rule_json).

    Renamed from CascadeExitNode (table was cascade_exit_nodes) when Platform split
    into RU-segment/foreign-segment instances made "exit_node_id: FK to nodes.id"
    structurally impossible to keep - the exit node's row lives in a different
    Platform's database once the two are genuinely split. No production data existed
    to preserve at rename time (see platform/README.md - only a handful of dogfood
    VDS, no real user base), so this is a clean rename rather than an additive one.
    """

    __tablename__ = "cascade_exit_links"

    cascade_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cascades.id", ondelete="CASCADE"), primary_key=True
    )
    remote_exit_candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("remote_exit_candidates.id"), primary_key=True
    )

    # The *peer's* ExitLinkAllocation.id for this same logical link, once a real peer
    # is involved (services/peering_client.py::provision_exit_link's response) - NULL
    # in the local/degenerate case (see RemoteExitCandidate's docstring) and while a
    # remote link hasn't been provisioned yet (services/link_negotiation.py, slice 12).
    remote_link_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Which Hop B preset is currently applied to THIS specific link - unlike
    # Cascade.hop_a_presets (Hop A, chosen once at creation, no runtime-switch path
    # yet), Hop B genuinely needs runtime switching for the preset-iteration retry
    # loop (services/link_negotiation.py, slice 12) since different exit candidates
    # can be mid-negotiation on different presets at the same time.
    active_preset: Mapped[str] = mapped_column(String(16), nullable=False, default="p1")
    status: Mapped[CascadeExitLinkStatus] = mapped_column(
        sa_enum(CascadeExitLinkStatus), nullable=False, default=CascadeExitLinkStatus.PENDING
    )

    # Hop B (entry->exit backhaul) REALITY identity for this specific link - PUBLIC
    # material only. The private key now lives on the exit side's own
    # ExitLinkAllocation.hopb_reality_private_key (generated by whichever Platform
    # will actually dispatch the matching AddInbound to its own exit-node Agent) -
    # RU-Platform never needs to see it, an incidental security improvement from the
    # split, not just a schema mechanic (see models/exit_link_allocation.py).
    hopb_reality_public_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hopb_reality_short_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    cascade: Mapped["Cascade"] = relationship(back_populates="exit_links")
    remote_exit_candidate: Mapped["RemoteExitCandidate"] = relationship()
