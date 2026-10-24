"""Incidents API: accepts alarms, returns 202 immediately, serves reports and traces.

Investigations take tens of seconds, so the API never runs them inline: it enqueues
a job and returns. Workers do the slow work. The API stays fast and can scale
independently of the workers.
"""

from __future__ import annotations

import hmac
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from common.context import request_id_var
from common.logging import configure_logging
from opspilot.api.schemas import (
    IncidentAccepted,
    IncidentIn,
    IncidentOut,
    ReportOut,
    ToolCallOut,
)
from opspilot.db.models import Incident, ToolCallRecord
from opspilot.db.queue import enqueue_incident
from opspilot.db.session import make_engine, make_session_factory
from opspilot.settings import OpsPilotSettings, get_settings

log = logging.getLogger("opspilot.api")


def _incident_out(incident: Incident) -> IncidentOut:
    out = IncidentOut.model_validate(incident, from_attributes=True)
    if incident.report is not None:
        out.report = ReportOut.model_validate(incident.report, from_attributes=True)
    return out


def create_app(
    settings: OpsPilotSettings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("opspilot-api", settings.log_level)
    sessions = session_factory or make_session_factory(make_engine(settings.database_url))
    app = FastAPI(title="OpsPilot", version="0.1.0")

    @app.middleware("http")
    async def correlate(request: Request, call_next: Callable[..., Any]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        log.info(
            f"{request.method} {getattr(route, 'path', request.url.path)} {response.status_code}",
            extra={
                "event": "http_request",
                "method": request.method,
                "path": getattr(route, "path", request.url.path),
                "status": response.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        response.headers["X-Request-ID"] = rid
        request_id_var.reset(token)
        return response

    def get_session() -> Iterator[Session]:
        with sessions() as session:
            yield session

    def require_secret(x_opspilot_secret: str = Header(default="")) -> None:
        expected = settings.api_secret.get_secret_value()
        if not expected or not hmac.compare_digest(x_opspilot_secret, expected):
            raise HTTPException(status_code=401, detail="invalid or missing X-OpsPilot-Secret")

    db = Depends(get_session)
    auth = [Depends(require_secret)]

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    def ready(session: Session = db) -> JSONResponse:
        try:
            session.execute(text("SELECT 1"))
        except OperationalError:
            return JSONResponse(status_code=503, content={"status": "unhealthy", "db": "down"})
        return JSONResponse(content={"status": "ok", "db": "ok"})

    @app.post("/incidents", dependencies=auth, response_model=IncidentAccepted, status_code=202)
    def create_incident(
        body: IncidentIn, response: Response, session: Session = db
    ) -> IncidentAccepted:
        incident, created = enqueue_incident(
            session,
            source=body.source,
            service=body.service,
            alarm_name=body.alarm_name,
            description=body.description,
            window_minutes=body.window_minutes or settings.default_window_minutes,
            alarm_time=body.alarm_time,
            max_attempts=settings.job_max_attempts,
        )
        if not created:
            response.status_code = 200
        log.info(
            "incident accepted" if created else "incident deduplicated",
            extra={"event": "incident_enqueued" if created else "incident_deduplicated"},
        )
        return IncidentAccepted(id=incident.id, status=incident.status, deduplicated=not created)

    @app.get("/incidents", dependencies=auth, response_model=list[IncidentOut])
    def list_incidents(
        session: Session = db,
        limit: int = Query(20, ge=1, le=100),
        status: Literal["queued", "investigating", "done", "failed"] | None = None,
    ) -> list[IncidentOut]:
        stmt = (
            select(Incident)
            .options(selectinload(Incident.report))
            .order_by(Incident.created_at.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(Incident.status == status)
        return [_incident_out(i) for i in session.scalars(stmt)]

    @app.get("/incidents/{incident_id}", dependencies=auth, response_model=IncidentOut)
    def get_incident(incident_id: uuid.UUID, session: Session = db) -> IncidentOut:
        incident = session.scalars(
            select(Incident)
            .options(selectinload(Incident.report))
            .where(Incident.id == incident_id)
        ).first()
        if incident is None:
            raise HTTPException(404, "incident not found")
        return _incident_out(incident)

    @app.get("/incidents/{incident_id}/trace", dependencies=auth, response_model=list[ToolCallOut])
    def get_trace(incident_id: uuid.UUID, session: Session = db) -> list[ToolCallOut]:
        if session.get(Incident, incident_id) is None:
            raise HTTPException(404, "incident not found")
        rows = session.scalars(
            select(ToolCallRecord)
            .where(ToolCallRecord.incident_id == incident_id)
            .order_by(ToolCallRecord.step)
        )
        return [ToolCallOut.model_validate(r, from_attributes=True) for r in rows]

    return app
