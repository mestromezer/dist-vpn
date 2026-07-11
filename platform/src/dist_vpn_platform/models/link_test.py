"""LinkTest - result of an on-demand test-request against a cascade (TZ-5 §3, §4.3).

Outcome enum matches contracts/proto/agent_control.proto TestOutcome (minus the
UNSPECIFIED sentinel) and contracts/rest/openapi.yaml TestOutcome exactly - the whole
point (TZ-5 §4.3) is surfacing *which step* failed, not a bare ok/fail.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from dist_vpn_platform.models.base import Base, new_uuid, sa_enum, utcnow


class TestOutcome(str, enum.Enum):
    SUCCESS = "success"
    CONTAINER_START_FAILED = "container_start_failed"
    TLS_HANDSHAKE_FAILED = "tls_handshake_failed"
    PROXY_NO_RESPONSE = "proxy_no_response"
    TIMEOUT = "timeout"


class LinkTest(Base):
    __tablename__ = "link_tests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cascade_id: Mapped[str] = mapped_column(String(36), ForeignKey("cascades.id"), nullable=False)

    # Correlates with agent_control.proto TestResult.test_id / Command.RunTest.test_id.
    test_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=new_uuid)

    outcome: Mapped[TestOutcome | None] = mapped_column(sa_enum(TestOutcome), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 'admin' or 'schedule' (openapi.yaml LinkTest.initiated_by).
    initiated_by: Mapped[str] = mapped_column(String(32), nullable=False, default="admin")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
