"""PeerPlatform - the other segment's Platform instance, once Platform is split into
an RU-segment (entry nodes) and a foreign-segment (exit nodes) instance (see
provisioning/README.md "Control-plane через REALITY" and platform/README.md for the
DPI-driven rationale: an RU-hosted entry node's control-plane channel to a Platform
outside Russia gets killed by ТСПУ mid-stream, so each Agent now only ever talks to
its own in-country Platform - the only traffic that still needs to cross the border is
the cascade's own data path plus this one Platform<->Platform peering channel).

A row here is a real, provisioned-once, operator-established peer identity - not a
config value - deliberately, for the same reason `Node` is a DB row rather than a
config entry: it needs a real local FK target for `RemoteExitCandidate`/
`ExitLinkAllocation` below, and its `status` needs to be a queryable, updatable fact
(last-known peering-channel health), not something re-read from a file each time.

In practice there is exactly one row on each side (one Foreign-Platform peer known to
RU-Platform, and vice versa) - nothing in the schema forces that, on purpose, so a
third segment would not need a schema change, only a second row.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow


class PeerPlatformStatus(str, enum.Enum):
    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


class PeerPlatform(Base):
    __tablename__ = "peer_platforms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    label: Mapped[str] = mapped_column(String(255), nullable=False)

    # host:port of the peer's PlatformPeering gRPC server (services/peering_client.py
    # dials this). Only ever set/used on the entry-segment side - per the peering
    # protocol's own design (grpc_server/peering_servicer.py module docstring),
    # RU-Platform always dials Foreign-Platform, never the reverse, so a foreign-
    # segment Platform's own PeerPlatform row(s) never need this populated.
    grpc_address: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Mutual cert pinning, NOT the Agent-facing mTLS CA (security/mtls_ca.py) - a
    # static, operator-established, exactly-one-peer relationship is the wrong shape
    # for that CA's 90-day auto-issued-off-a-one-time-token design, and reusing one CA
    # for both would make a compromised Agent enrollment flow equal-trust to "the
    # other Platform" - see security/peering_identity.py for the generation side.
    peer_cert_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_peering_key_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_peering_cert_pem: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Health of the peering CHANNEL itself (can RU-Platform currently reach this
    # peer's PlatformPeering server at all) - distinct from, and does not drive, any
    # individual CascadeExitLink's own status (models/cascade.py).
    status: Mapped[PeerPlatformStatus] = mapped_column(
        sa_enum(PeerPlatformStatus), nullable=False, default=PeerPlatformStatus.UNKNOWN
    )
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
