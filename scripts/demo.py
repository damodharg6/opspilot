"""Showcase demos: stage a real incident on the running stack and capture the result.

One run = reset the demo -> healthy traffic -> fault on -> traffic continues ->
alarm (POST /incidents) -> a worker investigates -> collect report + tool trace.
Everything goes through the real API, queue, worker, MCP servers and LLM.

Used by scripts/record_demos.py (recorded runs for the public site) and
scripts/showcase_server.py (live runs during a presentation).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from evals.runner import load_scenarios
from scripts import deploy_history, inject, load
from shop.main import SEED_PRODUCTS
from shop.models import Product

ALARM_NAME = "shop-health-degraded"
FINISHED = ("done", "failed")


@dataclass(frozen=True)
class Showcase:
    """How a scenario is presented on the site."""

    title: str
    tagline: str
    story: str


SHOWCASES: dict[str, Showcase] = {
    "bad_deploy_basic": Showcase(
        "Buggy deploy",
        "A release breaks checkout",
        "Release v1.5.0 changes order pricing. Orders for products without a promotion "
        "now fail with a KeyError. Can the agent connect the errors to the commit?",
    ),
    "db_latency_basic": Showcase(
        "Slow database",
        "Everything is slow, nothing is broken",
        "Every database query takes over a second. Almost no errors, so error counts "
        "alone say nothing. The agent has to read latency instead.",
    ),
    "business_stock_exhaustion": Showcase(
        "Stock exhaustion",
        "Not an outage at all",
        "One product sells out and customers get 409 'insufficient stock'. The system "
        "is healthy. A good on-call engineer says so instead of paging the DB team.",
    ),
    "memory_leak_basic": Showcase(
        "Memory leak",
        "Requests succeed while memory climbs",
        "Each request leaks memory. Nothing fails yet. The only signal is resident "
        "memory rising in the service's runtime stats.",
    ),
}
SCENARIO_IDS = tuple(SHOWCASES)

# Live presentations use a shorter healthy warmup; recorded runs use the eval suite's.
LIVE_WARMUP_S = 60.0

Progress = Callable[[str, str], None]


class DemoSettings(BaseSettings):
    """Reads the same .env as the stack."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    shop_url: str = "http://localhost:8000"
    api_url: str = "http://localhost:8080"
    opspilot_api_secret: SecretStr = SecretStr("")
    postgres_password: SecretStr = SecretStr("")
    db_host_port: int = 5432
    opspilot_agent_timeout_s: float = 90.0

    @property
    def shop_db_url(self) -> str:
        password = self.postgres_password.get_secret_value()
        return f"postgresql+psycopg://app:{password}@127.0.0.1:{self.db_host_port}/app"


@dataclass
class DemoRun:
    scenario: dict[str, Any]
    traffic: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    incident: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        show = SHOWCASES[self.scenario["id"]]
        report = self.incident.get("report") or {}
        return {
            "id": self.scenario["id"],
            "title": show.title,
            "tagline": show.tagline,
            "story": show.story,
            "fault": self.scenario.get("fault"),
            "params": self.scenario.get("params") or {},
            "expected": self.scenario["expected"],
            "correct": report.get("category") == self.scenario["expected"],
            "traffic": self.traffic,
            "timings": self.timings,
            "incident": self.incident,
            "trace": self.trace,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }


def scenario(scenario_id: str, live: bool = False) -> dict[str, Any]:
    if scenario_id not in SHOWCASES:
        raise KeyError(f"unknown demo scenario '{scenario_id}'")
    found = next(s for s in load_scenarios() if s["id"] == scenario_id)
    return {**found, "warmup_s": LIVE_WARMUP_S} if live else found


# --- resetting the demo ------------------------------------------------------------------
def restock(settings: DemoSettings) -> None:
    """Put stock back to the seed values: every order permanently uses some up."""
    engine = create_engine(settings.shop_db_url)
    try:
        with Session(engine) as session:
            for name, _, stock in SEED_PRODUCTS:
                session.execute(update(Product).where(Product.name == name).values(stock=stock))
            session.commit()
    finally:
        engine.dispose()


def reset_deploy_history() -> None:
    """Fresh history each run; the folder itself stays because mcp-git mounts it."""
    repo = deploy_history.REPO
    repo.mkdir(parents=True, exist_ok=True)
    for child in repo.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    deploy_history.init(force=True)


def reset_demo(settings: DemoSettings) -> None:
    inject.reset()
    restock(settings)
    reset_deploy_history()


# --- one run -------------------------------------------------------------------------------
async def raise_incident(client: httpx.AsyncClient, window_minutes: int) -> str:
    resp = await client.post(
        "/incidents",
        json={
            "alarm_name": ALARM_NAME,
            "window_minutes": window_minutes,
            "description": "shop health degraded (staged demo)",
        },
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


async def wait_for_report(
    client: httpx.AsyncClient, incident_id: str, timeout_s: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        resp = await client.get(f"/incidents/{incident_id}")
        resp.raise_for_status()
        incident = resp.json()
        if incident["status"] in FINISHED:
            return incident
        if time.monotonic() > deadline:
            raise TimeoutError(f"incident {incident_id} still '{incident['status']}'")
        await asyncio.sleep(2)


async def run_demo(
    scenario_id: str,
    *,
    live: bool = False,
    settings: DemoSettings | None = None,
    progress: Progress = lambda phase, detail: None,
) -> dict[str, Any]:
    settings = settings or DemoSettings()
    run = DemoRun(scenario(scenario_id, live))
    s = run.scenario
    headers = {"X-OpsPilot-Secret": settings.opspilot_api_secret.get_secret_value()}

    progress("reset", "restock shop, fresh deploy history, all faults off")
    await asyncio.to_thread(reset_demo, settings)
    try:
        progress("warmup", f"healthy traffic for {s['warmup_s']:.0f}s at {s['rate']} req/s")
        run.traffic["warmup"] = await load.run(settings.shop_url, s["rate"], s["warmup_s"])

        if s.get("fault"):
            await asyncio.to_thread(inject.enable, s["fault"], s.get("params"))
        progress("fault", f"fault '{s.get('fault')}' on, traffic continues for {s['fault_s']:.0f}s")
        run.traffic["fault"] = await load.run(settings.shop_url, s["rate"], s["fault_s"])

        async with httpx.AsyncClient(base_url=settings.api_url, headers=headers, timeout=10) as api:
            progress("alarm", f"POST /incidents alarm={ALARM_NAME}")
            incident_id = await raise_incident(api, int(s["window_minutes"]))
            progress("investigating", f"incident {incident_id} queued; a worker is investigating")
            started = time.monotonic()
            run.incident = await wait_for_report(
                api, incident_id, settings.opspilot_agent_timeout_s + 120
            )
            run.timings["investigation_s"] = round(time.monotonic() - started, 1)
            trace = await api.get(f"/incidents/{incident_id}/trace")
            trace.raise_for_status()
            run.trace = trace.json()
    finally:
        await asyncio.to_thread(inject.reset)

    run.timings.update(warmup_s=s["warmup_s"], fault_s=s["fault_s"])
    report = run.incident.get("report") or {}
    progress("done", f"{run.incident['status']}: {report.get('category', 'no report')}")
    return run.to_record()
