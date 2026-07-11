"""Unit tests for the AgentControl gRPC servicer's Enrollment/Heartbeat handling
(TZ-5 scope item 5) - exercised directly (no live gRPC channel) since the DB-facing
logic is what this slice needs to prove; the transport itself is generated/boilerplate
grpcio-tools code (contracts/proto/agent_control.proto)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.grpc_server.servicer import AgentControlServicer
from dist_vpn_platform.models.enrollment_token import EnrollmentToken
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.security.enrollment_tokens import generate_token, hash_token


async def _seed_enrollment_token() -> str:
    plaintext = generate_token()
    async with session_scope() as db:
        db.add(
            EnrollmentToken(
                token_hash=hash_token(plaintext),
                requested_role=NodeRole.ENTRY,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
        )
    return plaintext


@pytest.mark.asyncio
async def test_enrollment_with_valid_token_creates_node(db_engine: None) -> None:
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()

    ack = await servicer._handle_enrollment(
        pb.Enrollment(
            enrollment_token=plaintext,
            hostname="ru-vds-01",
            agent_version="0.1.0",
            requested_role=pb.NODE_ROLE_ENTRY,
        ),
        peer="test-peer",
    )

    assert ack.accepted is True
    assert ack.node_id

    async with session_scope() as db:
        node = await db.get(Node, ack.node_id)
        assert node is not None
        assert node.status == NodeStatus.ENROLLING
        assert node.hostname == "ru-vds-01"

        token_row = (await db.execute(select(EnrollmentToken))).scalar_one()
        assert token_row.used_at is not None


@pytest.mark.asyncio
async def test_enrollment_with_unknown_token_is_rejected(db_engine: None) -> None:
    servicer = AgentControlServicer()
    ack = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token="not-a-real-token", requested_role=pb.NODE_ROLE_ENTRY),
        peer="test-peer",
    )
    assert ack.accepted is False
    assert ack.reason


@pytest.mark.asyncio
async def test_enrollment_token_cannot_be_reused(db_engine: None) -> None:
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()

    first = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_EXIT), peer="peer-a"
    )
    assert first.accepted is True

    second = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_EXIT), peer="peer-b"
    )
    assert second.accepted is False
    assert "used" in second.reason.lower()


@pytest.mark.asyncio
async def test_heartbeat_transitions_enrolling_to_healthy(db_engine: None) -> None:
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()
    ack = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_ENTRY), peer="test-peer"
    )

    await servicer._handle_heartbeat(
        pb.Heartbeat(
            node_id=ack.node_id,
            health=pb.NodeHealth(xray_container_running=True, cpu_percent=12.5, mem_percent=30.0, disk_percent=40.0),
            timestamp_unix=int(time.time()),
        )
    )

    async with session_scope() as db:
        node = await db.get(Node, ack.node_id)
        assert node.status == NodeStatus.HEALTHY
        assert node.last_heartbeat_at is not None
        assert node.cpu_percent == 12.5

        events = (
            await db.execute(select(HealthEvent).where(HealthEvent.node_id == ack.node_id))
        ).scalars().all()
        assert len(events) == 1
        assert events[0].from_status == "enrolling"
        assert events[0].to_status == "healthy"
