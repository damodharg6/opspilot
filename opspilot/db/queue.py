"""Job queue on Postgres: enqueue with dedup, claim with SKIP LOCKED, leases, retries.

Guarantees:
- Dedup: one open incident per (service, alarm). Enforced by a partial unique
  index, so two alarms arriving at the same instant cannot both create a job.
- Claim: `FOR UPDATE SKIP LOCKED` lets any number of workers pull jobs
  concurrently without blocking each other or taking the same job.
- Lease: a claimed job carries `locked_until`. If a worker dies, the lease expires
  and another worker re-claims the job (at-least-once delivery).
- Fencing: results are only written by the worker that currently holds the lease,
  so a slow, expired worker cannot overwrite a newer attempt.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from opspilot.agent.investigator import InvestigationResult
from opspilot.agent.prompts import PROMPT_VERSION
from opspilot.db.models import OPEN_STATUSES, Incident, Report, ToolCallRecord, utcnow

RESULT_PREVIEW_CHARS = 4000


def dedup_key(service: str, alarm_name: str) -> str:
    return f"{service}:{alarm_name}"


def enqueue_incident(
    session: Session,
    *,
    source: str,
    service: str,
    alarm_name: str,
    description: str,
    window_minutes: int,
    alarm_time: datetime | None,
    max_attempts: int,
) -> tuple[Incident, bool]:
    """Create a queued incident, or return the open one for the same alarm.

    Returns (incident, created).
    """
    key = dedup_key(service, alarm_name)
    for _ in range(3):  # the open incident may close between insert and select: retry
        now = utcnow()
        stmt = (
            insert(Incident)
            .values(
                id=uuid.uuid4(),
                source=source,
                service=service,
                alarm_name=alarm_name,
                description=description,
                window_minutes=window_minutes,
                alarm_time=alarm_time,
                dedup_key=key,
                status="queued",
                attempts=0,
                max_attempts=max_attempts,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["dedup_key"],
                index_where=Incident.status.in_(OPEN_STATUSES),
            )
            .returning(Incident.id)
        )
        new_id = session.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            session.commit()
            return session.get_one(Incident, new_id), True
        existing = session.scalars(
            select(Incident).where(Incident.dedup_key == key, Incident.status.in_(OPEN_STATUSES))
        ).first()
        session.commit()
        if existing is not None:
            return existing, False
    raise RuntimeError(f"could not enqueue incident for {key}")


def claim_next(session: Session, worker_id: str, lease_s: int) -> Incident | None:
    """Atomically take the oldest available job (or one whose lease expired)."""
    now = utcnow()
    candidate = (
        select(Incident.id)
        .where(
            or_(
                (Incident.status == "queued") & (Incident.available_at <= now),
                (Incident.status == "investigating") & (Incident.locked_until < now),
            )
        )
        .order_by(Incident.available_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    claimed = session.scalars(
        update(Incident)
        .where(Incident.id == candidate)
        .values(
            status="investigating",
            locked_by=worker_id,
            locked_until=now + timedelta(seconds=lease_s),
            attempts=Incident.attempts + 1,
            updated_at=now,
        )
        .returning(Incident)
    ).first()
    session.commit()
    return claimed


def _owned(session: Session, incident_id: uuid.UUID, worker_id: str) -> Incident | None:
    """Lock the row and confirm this worker still holds the lease (fencing)."""
    incident = session.get(Incident, incident_id, with_for_update=True)
    if incident is None or incident.status != "investigating" or incident.locked_by != worker_id:
        return None
    return incident


def complete(
    session: Session, incident_id: uuid.UUID, worker_id: str, result: InvestigationResult
) -> bool:
    incident = _owned(session, incident_id, worker_id)
    if incident is None:
        session.rollback()
        return False
    report = result.report
    session.merge(
        Report(
            incident_id=incident_id,
            result_status=result.status,
            category=report.root_cause_category,
            summary=report.summary,
            root_cause=report.root_cause,
            confidence=report.confidence,
            evidence=[e.model_dump() for e in report.evidence],
            suggested_fix=report.suggested_fix,
            model=result.model,
            prompt_version=PROMPT_VERSION,
            stop_reason=result.stop_reason,
            tool_call_count=len(result.steps),
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            latency_s=result.latency_s,
            evidence_dropped=result.evidence_dropped,
            error=result.error,
        )
    )
    session.execute(
        ToolCallRecord.__table__.delete().where(ToolCallRecord.incident_id == incident_id)
    )
    for step in result.steps:
        data = asdict(step)
        session.add(
            ToolCallRecord(
                incident_id=incident_id,
                step=data["step"],
                tool=data["tool"],
                arguments=data["arguments"],
                result_preview=data["result"][:RESULT_PREVIEW_CHARS],
                is_error=data["is_error"],
                duration_ms=data["duration_ms"],
            )
        )
    incident.status = "done" if result.status != "failed" else "failed"
    incident.last_error = result.error
    incident.locked_by = None
    incident.locked_until = None
    incident.finished_at = utcnow()
    session.commit()
    return True


def fail_or_retry(session: Session, incident_id: uuid.UUID, worker_id: str, error: str) -> str:
    """Infrastructure error (not a model answer): retry with backoff, or give up."""
    incident = _owned(session, incident_id, worker_id)
    if incident is None:
        session.rollback()
        return "lost_lease"
    incident.last_error = error[:2000]
    incident.locked_by = None
    incident.locked_until = None
    if incident.attempts >= incident.max_attempts:
        incident.status = "failed"
        incident.finished_at = utcnow()
        outcome = "failed"
    else:
        incident.status = "queued"
        incident.available_at = utcnow() + timedelta(seconds=10 * 2 ** (incident.attempts - 1))
        outcome = "retry_scheduled"
    session.commit()
    return outcome


def extend_lease(session: Session, incident_id: uuid.UUID, worker_id: str, lease_s: int) -> bool:
    updated = session.execute(
        update(Incident)
        .where(Incident.id == incident_id, Incident.locked_by == worker_id)
        .values(locked_until=utcnow() + timedelta(seconds=lease_s))
    )
    session.commit()
    return updated.rowcount == 1  # type: ignore[attr-defined]
