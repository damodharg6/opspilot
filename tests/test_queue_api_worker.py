"""Queue, API and worker tests against a real Postgres (TEST_DATABASE_URL).

SKIP LOCKED, partial unique indexes and ON CONFLICT are Postgres behaviour, so
these are tested against Postgres, not mocked.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text, update
from sqlalchemy.orm import Session, sessionmaker

from mcp_servers.backends.local import LocalLogsBackend
from mcp_servers.logs_server import build_logs_server
from opspilot.agent.investigator import InvestigationResult
from opspilot.agent.report import IncidentReport
from opspilot.agent.tools import MCPToolGateway
from opspilot.api.app import create_app
from opspilot.db import queue
from opspilot.db.models import Incident, utcnow
from opspilot.db.session import make_engine, make_session_factory
from opspilot.llm.testing import ScriptedLLM
from opspilot.settings import OpsPilotSettings
from opspilot.worker.main import Worker
from tests.test_agent import answer, tool_call
from tests.test_mcp_servers import clock, log_file  # noqa: F401 - fixture reuse

pytestmark = pytest.mark.postgres
SECRET = {"X-OpsPilot-Secret": "test-secret"}


@pytest.fixture(scope="module")
def migrated_url() -> str:
    import os

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS opspilot CASCADE"))
    cfg = Config("alembic.ini")
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")  # the real migration, not create_all
    return url


@pytest.fixture()
def sessions(migrated_url: str) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(migrated_url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE opspilot.incidents CASCADE"))
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture()
def settings(migrated_url: str) -> OpsPilotSettings:
    return OpsPilotSettings(
        database_url=migrated_url, api_secret=SecretStr("test-secret"), job_max_attempts=2
    )


def enqueue(sessions: sessionmaker[Session], alarm: str = "shop-5xx") -> tuple[Incident, bool]:
    with sessions() as s:
        return queue.enqueue_incident(
            s,
            source="test",
            service="shop",
            alarm_name=alarm,
            description="",
            window_minutes=15,
            alarm_time=None,
            max_attempts=2,
        )


def done_result() -> InvestigationResult:
    return InvestigationResult(
        status="completed", report=IncidentReport.unknown("x"), model="m", stop_reason="answered"
    )


# --- queue ---------------------------------------------------------------------------------
def test_enqueue_dedups_open_incidents(sessions: sessionmaker[Session]) -> None:
    first, created1 = enqueue(sessions)
    second, created2 = enqueue(sessions)
    other, created3 = enqueue(sessions, alarm="shop-latency")
    assert created1 and not created2 and created3
    assert second.id == first.id and other.id != first.id


def test_closed_incident_allows_new_one(sessions: sessionmaker[Session]) -> None:
    first, _ = enqueue(sessions)
    with sessions() as s:
        claimed = queue.claim_next(s, "w1", 60)
        assert queue.complete(s, claimed.id, "w1", done_result())
    second, created = enqueue(sessions)
    assert created and second.id != first.id


def test_skip_locked_gives_concurrent_workers_different_jobs(
    sessions: sessionmaker[Session],
) -> None:
    enqueue(sessions, "a")
    enqueue(sessions, "b")
    s1, s2 = sessions(), sessions()
    try:
        # Hold a row lock in s1 the way a slow claimer would, then claim from s2.
        locked = s1.execute(
            text("SELECT id FROM opspilot.incidents ORDER BY available_at LIMIT 1 FOR UPDATE")
        ).scalar_one()
        got = queue.claim_next(s2, "w2", 60)
        assert got is not None and got.id != locked  # skipped the locked row, did not wait
    finally:
        s1.rollback()
        s1.close()
        s2.close()


def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced(
    sessions: sessionmaker[Session],
) -> None:
    enqueue(sessions)
    with sessions() as s:
        job = queue.claim_next(s, "w1", 60)
        s.execute(
            update(Incident)
            .where(Incident.id == job.id)
            .values(locked_until=utcnow() - timedelta(seconds=1))
        )
        s.commit()
        again = queue.claim_next(s, "w2", 60)
        assert again.id == job.id and again.attempts == 2
        assert queue.complete(s, job.id, "w1", done_result()) is False  # w1 lost its lease
        assert queue.complete(s, job.id, "w2", done_result()) is True


def test_retry_then_fail(sessions: sessionmaker[Session]) -> None:
    enqueue(sessions)
    with sessions() as s:
        job = queue.claim_next(s, "w1", 60)
        assert queue.fail_or_retry(s, job.id, "w1", "boom") == "retry_scheduled"
        assert queue.claim_next(s, "w1", 60) is None  # backoff: not available yet
        s.execute(update(Incident).values(available_at=utcnow()))
        s.commit()
        job = queue.claim_next(s, "w1", 60)
        assert queue.fail_or_retry(s, job.id, "w1", "boom") == "failed"


# --- API -----------------------------------------------------------------------------------
@pytest.fixture()
def api(settings: OpsPilotSettings, sessions: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(create_app(settings, sessions)) as client:
        yield client


def test_api_requires_secret(api: TestClient) -> None:
    assert api.post("/incidents", json={"alarm_name": "x"}).status_code == 401
    assert api.get("/incidents", headers={"X-OpsPilot-Secret": "wrong"}).status_code == 401
    assert api.get("/health").json()["db"] == "ok"


def test_api_create_dedup_and_read(api: TestClient) -> None:
    r1 = api.post("/incidents", headers=SECRET, json={"alarm_name": "shop-5xx"})
    r2 = api.post("/incidents", headers=SECRET, json={"alarm_name": "shop-5xx"})
    assert r1.status_code == 202 and r2.status_code == 200
    assert r2.json() == {"id": r1.json()["id"], "status": "queued", "deduplicated": True}
    got = api.get(f"/incidents/{r1.json()['id']}", headers=SECRET).json()
    assert got["status"] == "queued" and got["report"] is None
    assert api.get(f"/incidents/{uuid.uuid4()}", headers=SECRET).status_code == 404
    assert (
        api.post(
            "/incidents", headers=SECRET, json={"alarm_name": "x", "window_minutes": 999}
        ).status_code
        == 422
    )


# --- worker end to end -----------------------------------------------------------------------
async def test_worker_investigates_and_api_serves_report(
    settings: OpsPilotSettings,
    sessions: sessionmaker[Session],
    api: TestClient,
    log_file: Path,  # noqa: F811
) -> None:
    incident_id = api.post("/incidents", headers=SECRET, json={"alarm_name": "shop-5xx"}).json()[
        "id"
    ]
    llm = ScriptedLLM([tool_call("search_logs", {"query": "status>=500"}), answer()])
    server = build_logs_server(LocalLogsBackend(log_file, clock))

    @asynccontextmanager
    async def gateway():
        async with MCPToolGateway({"logs": server}) as g:
            yield g

    worker = Worker(settings, sessions, llm, gateway, worker_id="w-test")
    assert await worker.process_one() is True
    assert await worker.process_one() is False  # queue empty

    got = api.get(f"/incidents/{incident_id}", headers=SECRET).json()
    assert got["status"] == "done"
    assert got["report"]["category"] == "bad_deploy"
    assert got["report"]["evidence"][0]["snippet"] == "POST /orders 500"
    trace = api.get(f"/incidents/{incident_id}/trace", headers=SECRET).json()
    assert trace[0]["tool"] == "search_logs"
    assert json.loads(trace[0]["result_preview"])["total_matched"] == 2


async def test_worker_retries_when_tools_are_down(
    settings: OpsPilotSettings, sessions: sessionmaker[Session], api: TestClient
) -> None:
    api.post("/incidents", headers=SECRET, json={"alarm_name": "shop-5xx"})

    @asynccontextmanager
    async def broken_gateway():
        raise ConnectionError("mcp-logs unreachable")
        yield  # pragma: no cover

    worker = Worker(settings, sessions, ScriptedLLM([]), broken_gateway, worker_id="w-test")
    await worker.process_one()
    incident = api.get("/incidents", headers=SECRET).json()[0]
    assert incident["status"] == "queued" and "unreachable" in incident["last_error"]
