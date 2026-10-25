"""Serve the showcase site locally, with live demo runs against the running stack.

    python -m scripts.showcase_server        # or: make showcase -> http://localhost:8090

The browser never sees the API secret: it asks this server to run a scenario and
the server talks to the OpsPilot API. Bound to 127.0.0.1 with a Host check, so
other machines and DNS-rebinding pages cannot start runs.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scripts.demo import SCENARIO_IDS, SHOWCASES, run_demo

SITE_DIR = Path(__file__).resolve().parent.parent / "showcase"
Runner = Callable[..., Awaitable[dict[str, Any]]]


class RunIn(BaseModel):
    scenario: str


@dataclass
class LiveRun:
    id: str
    scenario: str
    state: str = "running"  # running | done | error
    events: list[dict[str, str]] = field(default_factory=list)
    record: dict[str, Any] | None = None
    error: str | None = None

    def add_event(self, phase: str, detail: str) -> None:
        at = datetime.now(UTC).isoformat(timespec="seconds")
        self.events.append({"phase": phase, "detail": detail, "at": at})


def create_app(runner: Runner = run_demo, site_dir: Path = SITE_DIR) -> FastAPI:
    app = FastAPI(title="OpsPilot showcase", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    runs: dict[str, LiveRun] = {}
    tasks: set[asyncio.Task[None]] = set()

    def active() -> LiveRun | None:
        return next((r for r in runs.values() if r.state == "running"), None)

    async def execute(run: LiveRun) -> None:
        try:
            run.record = await runner(run.scenario, live=True, progress=run.add_event)
            run.state = "done"
        except Exception as exc:  # shown to the presenter instead of failing silently
            run.error = f"{type(exc).__name__}: {exc}"[:500]
            run.state = "error"

    @app.get("/live/status")
    def status() -> dict[str, Any]:
        current = active()
        return {
            "live": True,
            "running": current.id if current else None,
            "scenarios": [{"id": sid, "title": SHOWCASES[sid].title} for sid in SCENARIO_IDS],
        }

    @app.post("/live/runs", status_code=202)
    async def start(body: RunIn) -> dict[str, str]:
        if body.scenario not in SHOWCASES:
            raise HTTPException(404, f"unknown scenario '{body.scenario}'")
        if (current := active()) is not None:
            raise HTTPException(409, f"run {current.id} ({current.scenario}) is still going")
        run = LiveRun(id=uuid.uuid4().hex[:12], scenario=body.scenario)
        runs[run.id] = run
        task = asyncio.create_task(execute(run))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"id": run.id}

    @app.get("/live/runs/{run_id}")
    def get_run(run_id: str) -> LiveRun:
        if (run := runs.get(run_id)) is None:
            raise HTTPException(404, "unknown run")
        return run

    app.mount("/", StaticFiles(directory=site_dir, html=True), name="site")
    return app


def main() -> None:
    port = int(os.getenv("SHOWCASE_PORT", "8090"))
    print(f"OpsPilot showcase on http://localhost:{port}")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
