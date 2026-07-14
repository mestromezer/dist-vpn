"""Unit tests for the AgentControl gRPC servicer's Enrollment/Heartbeat handling
(TZ-5 scope item 5) - exercised directly (no live gRPC channel) since the DB-facing
logic is what this slice needs to prove; the transport itself is generated/boilerplate
grpcio-tools code (contracts/proto/agent_control.proto)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from dist_vpn_platform.db import session_scope
from dist_vpn_platform.generated import agent_control_pb2 as pb
from dist_vpn_platform.grpc_server.servicer import AgentControlServicer
from dist_vpn_platform.models.cascade import Cascade, CascadeStatus
from dist_vpn_platform.models.enrollment_token import EnrollmentToken
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.node import Node, NodeRole, NodeStatus
from dist_vpn_platform.models.node_resource_sample import NodeResourceSample
from dist_vpn_platform.models.usage_stat import UsageStat
from dist_vpn_platform.models.user import User, UserCascade
from dist_vpn_platform.models.user_device_kick_event import UserDeviceKickEvent
from dist_vpn_platform.security.enrollment_tokens import generate_token, hash_token
from tests.grpc_test_helpers import FakeAgent, generate_test_csr_pem


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
            csr_pem=generate_test_csr_pem("ru-vds-01"),
        ),
        peer="test-peer",
    )

    assert ack.accepted is True
    assert ack.node_id
    assert ack.mtls_client_cert_pem  # TZ-5 §6.2: real cert PEM, no longer an empty placeholder

    async with session_scope() as db:
        node = await db.get(Node, ack.node_id)
        assert node is not None
        assert node.status == NodeStatus.ENROLLING
        assert node.hostname == "ru-vds-01"
        assert node.mtls_cert_fingerprint  # populated from the issued client cert

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
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_EXIT, csr_pem=generate_test_csr_pem()),
        peer="peer-a",
    )
    assert first.accepted is True

    second = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_EXIT, csr_pem=generate_test_csr_pem()),
        peer="peer-b",
    )
    assert second.accepted is False
    assert "used" in second.reason.lower()


@pytest.mark.asyncio
async def test_heartbeat_transitions_enrolling_to_healthy(db_engine: None) -> None:
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()
    ack = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_ENTRY, csr_pem=generate_test_csr_pem()),
        peer="test-peer",
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


@pytest.mark.asyncio
async def test_heartbeat_recovers_down_cascade_on_entry_node(db_engine: None) -> None:
    """Real bug fix, not a hypothetical: services/health_scheduler.py can mark a
    cascade DOWN (entry node heartbeat timeout), but nothing ever flipped it back to
    UP when the entry node's heartbeat resumed - it would have stayed DOWN forever.
    _handle_heartbeat now does this itself, in the same place it already recovers the
    Node's own status."""
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()
    ack = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_ENTRY, csr_pem=generate_test_csr_pem()),
        peer="test-peer",
    )

    async with session_scope() as db:
        node = await db.get(Node, ack.node_id)
        node.status = NodeStatus.DOWN  # simulate a prior health_scheduler timeout
        cascade = Cascade(entry_node_id=ack.node_id, status=CascadeStatus.DOWN)
        already_up_cascade = Cascade(entry_node_id=ack.node_id, status=CascadeStatus.UP)
        db.add(cascade)
        db.add(already_up_cascade)
        await db.flush()
        cascade_id, already_up_id = cascade.id, already_up_cascade.id

    await servicer._handle_heartbeat(
        pb.Heartbeat(
            node_id=ack.node_id,
            health=pb.NodeHealth(xray_container_running=True, cpu_percent=1.0, mem_percent=1.0, disk_percent=1.0),
            timestamp_unix=int(time.time()),
        )
    )

    async with session_scope() as db:
        node = await db.get(Node, ack.node_id)
        assert node.status == NodeStatus.HEALTHY

        cascade = await db.get(Cascade, cascade_id)
        assert cascade.status == CascadeStatus.UP

        cascade_events = (
            await db.execute(select(HealthEvent).where(HealthEvent.cascade_id == cascade_id))
        ).scalars().all()
        assert len(cascade_events) == 1
        assert cascade_events[0].from_status == "down"
        assert cascade_events[0].to_status == "up"

        # An already-UP cascade on the same node must not get a spurious duplicate
        # transition event just because the node itself also recovered.
        already_up_events = (
            await db.execute(select(HealthEvent).where(HealthEvent.cascade_id == already_up_id))
        ).scalars().all()
        assert already_up_events == []


@pytest.mark.asyncio
async def test_telemetry_persists_usage_stats_and_resolves_known_user(db_engine: None) -> None:
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()
    ack = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_ENTRY, csr_pem=generate_test_csr_pem()),
        peer="test-peer",
    )

    async with session_scope() as db:
        user = User(name="dave")
        db.add(user)
        await db.flush()
        user_uuid = user.uuid

    timestamp = int(datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc).timestamp())
    await servicer._handle_telemetry(
        pb.TelemetryReport(
            node_id=ack.node_id,
            timestamp_unix=timestamp,
            counters=[
                pb.UsageCounter(scope="user", key=user_uuid, uplink_bytes=1000, downlink_bytes=2000),
                pb.UsageCounter(scope="inbound", key="hopa-in-test", uplink_bytes=50, downlink_bytes=60),
            ],
        )
    )
    # A second report in the same hourly bucket must accumulate, not duplicate rows.
    await servicer._handle_telemetry(
        pb.TelemetryReport(
            node_id=ack.node_id,
            timestamp_unix=timestamp + 60,
            counters=[pb.UsageCounter(scope="user", key=user_uuid, uplink_bytes=100, downlink_bytes=200)],
        )
    )

    async with session_scope() as db:
        rows = (await db.execute(select(UsageStat).where(UsageStat.node_id == ack.node_id))).scalars().all()
        by_scope = {row.scope: row for row in rows}

        assert len(rows) == 2  # one per scope/key, accumulated - not one per report

        user_row = by_scope["user"]
        assert user_row.raw_key == user_uuid
        assert user_row.user_id == (await db.execute(select(User.id).where(User.uuid == user_uuid))).scalar_one()
        assert user_row.uplink_bytes == 1100
        assert user_row.downlink_bytes == 2200

        inbound_row = by_scope["inbound"]
        assert inbound_row.user_id is None
        assert inbound_row.uplink_bytes == 50
        assert inbound_row.downlink_bytes == 60


@pytest.mark.asyncio
async def test_heartbeat_writes_node_resource_sample(db_engine: None) -> None:
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()
    ack = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_ENTRY, csr_pem=generate_test_csr_pem()),
        peer="test-peer",
    )

    async with session_scope() as db:
        cascade = Cascade(entry_node_id=ack.node_id, status=CascadeStatus.UP)
        db.add(cascade)
        await db.flush()
        for name in ("alice", "bob"):
            user = User(name=name)
            db.add(user)
            await db.flush()
            db.add(UserCascade(user_id=user.id, cascade_id=cascade.id))

    await servicer._handle_heartbeat(
        pb.Heartbeat(
            node_id=ack.node_id,
            health=pb.NodeHealth(xray_container_running=True, cpu_percent=55.5, mem_percent=60.0, disk_percent=20.0),
            timestamp_unix=int(time.time()),
        )
    )

    async with session_scope() as db:
        samples = (
            await db.execute(select(NodeResourceSample).where(NodeResourceSample.node_id == ack.node_id))
        ).scalars().all()
        assert len(samples) == 1
        sample = samples[0]
        assert sample.cpu_percent == 55.5
        assert sample.mem_percent == 60.0
        assert sample.disk_percent == 20.0
        # Two Users with a UserCascade into a cascade whose entry_node_id is this
        # node - provisioned, not online (no TelemetryReport.user_online arrived yet
        # in this test, so online counts stay at their zero default).
        assert sample.provisioned_user_count == 2
        assert sample.online_user_count == 0
        assert sample.online_device_count == 0

    # A second heartbeat must add a second row, not overwrite the first - this is
    # the whole point of a time series (Node.cpu_percent/etc. already behave as a
    # latest-value-only snapshot; NodeResourceSample exists specifically not to).
    await servicer._handle_heartbeat(
        pb.Heartbeat(
            node_id=ack.node_id,
            health=pb.NodeHealth(xray_container_running=True, cpu_percent=70.0, mem_percent=65.0, disk_percent=21.0),
            timestamp_unix=int(time.time()) + 15,
        )
    )
    async with session_scope() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(NodeResourceSample).where(NodeResourceSample.node_id == ack.node_id)
            )
        ).scalar_one()
        assert count == 2


@pytest.mark.asyncio
async def test_telemetry_user_online_caches_device_count_without_kicking(db_engine: None) -> None:
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()
    ack = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_ENTRY, csr_pem=generate_test_csr_pem()),
        peer="test-peer",
    )

    async with session_scope() as db:
        user = User(name="carol", max_devices=3)
        db.add(user)
        await db.flush()
        user_uuid = user.uuid

    await servicer._handle_telemetry(
        pb.TelemetryReport(
            node_id=ack.node_id,
            timestamp_unix=int(time.time()),
            user_online=[pb.UserOnlineStat(email=user_uuid, online_ips=["10.0.0.1", "10.0.0.2"])],
        )
    )

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.uuid == user_uuid))).scalar_one()
        assert user.last_online_device_count == 2
        assert user.last_online_at is not None

        kicks = (
            await db.execute(select(UserDeviceKickEvent).where(UserDeviceKickEvent.user_id == user.id))
        ).scalars().all()
        assert kicks == []  # 2 <= max_devices=3, no kick

        node = await db.get(Node, ack.node_id)
        assert node.last_online_user_count == 1
        assert node.last_online_device_count == 2


@pytest.mark.asyncio
async def test_telemetry_user_online_over_limit_kicks_and_records_event(db_engine: None) -> None:
    """The real end-to-end path: a FakeAgent (live SessionRegistry-bound node, see
    grpc_test_helpers.py) is granted a User whose online-IP count then exceeds
    max_devices - the servicer must dispatch a real RemoveUser+AddUser pair back to
    that same connected Agent (forcing every device to reconnect) and persist an
    audit row, not just update the cached count."""
    agent = FakeAgent()
    try:
        node_id = await agent.connect(NodeRole.ENTRY)

        async with session_scope() as db:
            cascade = Cascade(entry_node_id=node_id, status=CascadeStatus.UP)
            db.add(cascade)
            await db.flush()
            cascade_id = cascade.id
            user = User(name="dave", max_devices=2)
            db.add(user)
            await db.flush()
            user_id = user.id
            user_uuid = user.uuid
            db.add(UserCascade(user_id=user.id, cascade_id=cascade.id))

        servicer = AgentControlServicer()
        await servicer._handle_telemetry(
            pb.TelemetryReport(
                node_id=node_id,
                timestamp_unix=int(time.time()),
                user_online=[
                    pb.UserOnlineStat(email=user_uuid, online_ips=["10.0.0.1", "10.0.0.2", "10.0.0.3"])
                ],
            )
        )

        # Real dispatch, not just a DB write - the fake Agent actually received and
        # acked a real RemoveUser+AddUser pair on the cascade's own inbound_tag.
        assert len(agent.received_commands) == 2
        remove_cmd, add_cmd = agent.received_commands
        assert remove_cmd.WhichOneof("op") == "remove_user"
        assert remove_cmd.remove_user.email == user_uuid
        assert remove_cmd.remove_user.inbound_tag == f"hopa-in-{cascade_id}"
        assert add_cmd.WhichOneof("op") == "add_user"
        assert add_cmd.add_user.email == user_uuid
        assert add_cmd.add_user.uuid == user_uuid
        assert add_cmd.add_user.inbound_tag == f"hopa-in-{cascade_id}"

        async with session_scope() as db:
            kicks = (
                await db.execute(select(UserDeviceKickEvent).where(UserDeviceKickEvent.user_id == user_id))
            ).scalars().all()
            assert len(kicks) == 1
            assert kicks[0].online_ip_count == 3
            assert kicks[0].max_devices == 2
            assert kicks[0].node_id == node_id
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_telemetry_user_online_unlimited_never_kicks(db_engine: None) -> None:
    plaintext = await _seed_enrollment_token()
    servicer = AgentControlServicer()
    ack = await servicer._handle_enrollment(
        pb.Enrollment(enrollment_token=plaintext, requested_role=pb.NODE_ROLE_ENTRY, csr_pem=generate_test_csr_pem()),
        peer="test-peer",
    )

    async with session_scope() as db:
        # max_devices=None is an explicit, deliberate "unlimited" opt-out (see
        # models/user.py User.max_devices docstring) - not the same as 0.
        user = User(name="erin", max_devices=None)
        db.add(user)
        await db.flush()
        user_uuid = user.uuid

    await servicer._handle_telemetry(
        pb.TelemetryReport(
            node_id=ack.node_id,
            timestamp_unix=int(time.time()),
            user_online=[
                pb.UserOnlineStat(email=user_uuid, online_ips=[f"10.0.0.{i}" for i in range(1, 21)])
            ],
        )
    )

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.uuid == user_uuid))).scalar_one()
        assert user.last_online_device_count == 20
        kicks = (
            await db.execute(select(UserDeviceKickEvent).where(UserDeviceKickEvent.user_id == user.id))
        ).scalars().all()
        assert kicks == []
