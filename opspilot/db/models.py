"""OpsPilot's own tables, in their own Postgres schema so they never mix with the app's data.

The incidents table is also the job queue: a row in status 'queued' is a job.
Using Postgres as the queue keeps the stack small; the queue module is the only
place that knows this, so it can be swapped for SQS later without touching the API.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SCHEMA = "opspilot"
OPEN_STATUSES = ("queued", "investigating")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        schema=SCHEMA,
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        },
    )


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'investigating', 'done', 'failed')", name="status_valid"
        ),
        # At most one open incident per alarm: enforced by the database, so it is race-safe.
        Index(
            "uq_incidents_open_dedup_key",
            "dedup_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'investigating')"),
        ),
        # The worker's claim query scans this index.
        Index("ix_incidents_status_available_at", "status", "available_at"),
        Index("ix_incidents_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(40))
    service: Mapped[str] = mapped_column(String(80))
    alarm_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    window_minutes: Mapped[int] = mapped_column(Integer)
    alarm_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedup_key: Mapped[str] = mapped_column(String(300))

    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    report: Mapped[Report | None] = relationship(back_populates="incident", uselist=False)
    tool_calls: Mapped[list[ToolCallRecord]] = relationship(
        back_populates="incident", order_by="ToolCallRecord.step"
    )


class Report(Base):
    __tablename__ = "reports"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True
    )
    result_status: Mapped[str] = mapped_column(String(20))  # completed | partial | failed
    category: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(10))
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    suggested_fix: Mapped[str] = mapped_column(Text)

    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    stop_reason: Mapped[str] = mapped_column(String(30))
    tool_call_count: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    latency_s: Mapped[float] = mapped_column(Float)
    evidence_dropped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="report")


class ToolCallRecord(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_incident_id_step", "incident_id", "step"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    step: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(80))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result_preview: Mapped[str] = mapped_column(Text)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[float] = mapped_column(Float)

    incident: Mapped[Incident] = relationship(back_populates="tool_calls")
