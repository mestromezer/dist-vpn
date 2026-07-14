"""Node - a member of the fleet pool (TZ-5 §3, §4.1). Role/status enums match the
values fixed by the contracts (contracts/rest/openapi.yaml NodeRole/NodeStatus and
contracts/proto/agent_control.proto NodeRole) - do not diverge from those."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow


class NodeRole(str, enum.Enum):
    ENTRY = "entry"
    EXIT = "exit"


class NodeStatus(str, enum.Enum):
    ENROLLING = "enrolling"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    DECOMMISSIONED = "decommissioned"


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Public dial address (IP or DNS name) a VPN client should connect to when this
    # node is an entry node - deliberately separate from `hostname` above, which is
    # self-reported by the Agent at enrollment (Enrollment.hostname, agent_control.proto)
    # and may be an internal/private hostname, not necessarily what's reachable from the
    # public internet. Only the operator knows the VDS's real public address, so this is
    # admin-set via PATCH /nodes/{id} (schemas/nodes.py NodePatchRequest), never inferred.
    # Required (only) to compose a client vless:// URI - see api/routes/users.py.
    public_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[NodeRole] = mapped_column(sa_enum(NodeRole), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[NodeStatus] = mapped_column(
        sa_enum(NodeStatus), nullable=False, default=NodeStatus.ENROLLING
    )
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Last reported NodeHealth (agent_control.proto) - used to derive `status` transitions
    # and surfaced to the Admin Client for the "what/where broke" requirement (TZ-5 §4.3).
    xray_container_running: Mapped[bool | None] = mapped_column(nullable=True)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_percent: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Cached from the most recent TelemetryReport.user_online this node sent
    # (grpc_server/servicer.py::_handle_user_online) - read when writing a
    # NodeResourceSample row on the next Heartbeat, since Heartbeat and
    # TelemetryReport are separate messages with no guaranteed arrival order/timing
    # relative to each other (see NodeResourceSample's own module docstring and
    # _handle_heartbeat for how this is used). Not authoritative for anything else -
    # purely a resource-vs-load correlation aid.
    last_online_user_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_online_device_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # mTLS identity (TZ-5 §6.2). Real step-ca issuance is out of scope for this slice -
    # see platform/README.md TODO section; column exists so the schema doesn't need to
    # change when that lands.
    mtls_cert_fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True)

    enrollment_token_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("enrollment_tokens.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    decommissioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    enrollment_token: Mapped["EnrollmentToken | None"] = relationship(back_populates="node")
