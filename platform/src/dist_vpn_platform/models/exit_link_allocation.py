"""ExitLinkAllocation - the exit-segment Platform's own record of "which of my exit
nodes serves which remote cascade" (see models/peer_platform.py for the split-Platform
context). This is the row Foreign-Platform's own Admin Client reads to show cascade/
health status for its own nodes, and its `id` becomes the `remote_link_id` that
RU-Platform stores on the matching `CascadeExitLink` (models/cascade.py) - the two
rows are two Platforms' independent views of the same logical link, correlated by that
id, never a shared foreign key (they may not even share a database).

`hopb_reality_private_key` is generated and lives HERE now, not on `CascadeExitLink` -
this is the Platform that will actually dispatch the matching Hop B `AddInbound` to
its own exit-node Agent, so it is also the only Platform that ever needs the private
key. RU-Platform receives only the public key/short_id back (over
services/peering_client.py's `ProvisionExitLink`/`SwitchLinkPreset` responses) - an
incidental security improvement over the pre-split design, where one Platform held
both hops' private keys simply because it happened to own both nodes.

Degenerate/local case (see api/routes/cascades.py::create_cascade): same pattern as
RemoteExitCandidate - `peer_platform_id` NULL means this allocation was created
locally (Platform not actually split yet, or exit node co-located with the entry
side), and `remote_cascade_id` is then a literal, resolvable local `cascades.id`;
`peer_platform_id` set means it is the peer's opaque `Cascade.id`, correlation/display
only.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow


class ExitLinkAllocationStatus(str, enum.Enum):
    PENDING = "pending"
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class ExitLinkAllocation(Base):
    __tablename__ = "exit_link_allocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # NULL = local/degenerate (see module docstring). Set = a real peer, and
    # remote_cascade_id below is that peer's opaque Cascade.id.
    peer_platform_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("peer_platforms.id"), nullable=True
    )
    # This exit node genuinely lives in THIS database either way - real FK.
    exit_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.id"), nullable=False)
    remote_cascade_id: Mapped[str] = mapped_column(String(36), nullable=False)

    hopb_reality_private_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hopb_reality_public_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hopb_reality_short_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    active_preset: Mapped[str] = mapped_column(String(16), nullable=False, default="p1")
    status: Mapped[ExitLinkAllocationStatus] = mapped_column(
        sa_enum(ExitLinkAllocationStatus), nullable=False, default=ExitLinkAllocationStatus.PENDING
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "peer_platform_id", "remote_cascade_id", "exit_node_id", name="uq_exit_link_allocation_peer_cascade_node"
        ),
    )
