"""RemoteExitCandidate - RU-Platform's own local record of "an exit node I know about,
because I have (or want) a cascade link to it" (see models/peer_platform.py for the
split-Platform context this exists for).

This is the piece that lets `CascadeExitLink` (models/cascade.py) keep a real, local
foreign-key target after the split: `Cascade.entry_node_id`/the old
`CascadeExitNode.exit_node_id` were both plain FKs into one shared `nodes` table in
one DB - that breaks the moment entry and exit nodes live in two Platforms' separate
databases, since the exit node's actual `Node` row simply does not exist in
RU-Platform's DB. `CascadeExitLink.remote_exit_candidate_id` now points here instead,
and this row carries just enough mirrored, best-effort, explicitly-NOT-authoritative
data (refreshed via services/peering_client.py's `ListExitLinks` calls) to render a
useful Admin Client view and to dispatch the Hop A->B `AddOutbound` locally.

Degenerate/local case (see api/routes/cascades.py::create_cascade): when Platform is
NOT actually split yet, or an exit node happens to be co-located in the same DB as the
entry node, `peer_platform_id` is left NULL and `remote_node_id` is a literal,
resolvable `nodes.id` in this same database - this is intentionally not a real SQL
FOREIGN KEY constraint (unlike `peer_platform_id`, which IS a real FK), precisely
because the moment `peer_platform_id` is set to a real peer, `remote_node_id` becomes
opaque - the *peer's* `Node.id`, not resolvable in this database at all. Both cases
share one column rather than two only-one-of-which-is-ever-set columns because every
piece of code that reads `remote_node_id` already has to branch on
`peer_platform_id is None` to know whether it may `db.get(Node, remote_node_id)` or
must instead treat it as opaque - splitting the column would not remove that branch,
only rename it.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow


class RemoteExitCandidateStatus(str, enum.Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


class RemoteExitCandidate(Base):
    __tablename__ = "remote_exit_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # NULL = remote_node_id below is a literal local nodes.id (degenerate/same-DB
    # case - see module docstring). Set = remote_node_id is the peer's own opaque
    # Node.id, mirrored data only, refreshed via peering, never locally resolvable.
    peer_platform_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("peer_platforms.id"), nullable=True
    )
    remote_node_id: Mapped[str] = mapped_column(String(36), nullable=False)

    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[RemoteExitCandidateStatus] = mapped_column(
        sa_enum(RemoteExitCandidateStatus), nullable=False, default=RemoteExitCandidateStatus.UNKNOWN
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # NOTE: with peer_platform_id NULL, Postgres/SQLite do not deduplicate NULLs
    # against each other in a UNIQUE constraint, so this does not actually prevent two
    # local rows from repeating the same remote_node_id - callers (create_cascade)
    # must still get-or-create explicitly for the degenerate case rather than relying
    # on this constraint to reject a duplicate insert. It IS enforced for real peers.
    __table_args__ = (
        UniqueConstraint("peer_platform_id", "remote_node_id", name="uq_remote_exit_candidate_peer_node"),
    )
