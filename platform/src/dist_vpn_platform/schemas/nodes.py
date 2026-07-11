"""See contracts/rest/openapi.yaml components/schemas/Node, NodeRole, NodeStatus and
paths /nodes, /nodes/{node_id}."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from dist_vpn_platform.models.node import NodeRole, NodeStatus


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str | None = None
    role: NodeRole
    region: str | None = None
    status: NodeStatus
    agent_version: str | None = None
    last_heartbeat_at: datetime | None = None


class NodeListResponse(BaseModel):
    items: list[NodeOut]
    total: int


class NodeEnrollRequest(BaseModel):
    requested_role: NodeRole
    label: str | None = None


class NodeEnrollResponse(BaseModel):
    enrollment_token: str
    expires_at: datetime


class NodePatchRequest(BaseModel):
    role: NodeRole | None = None
    label: str | None = None
