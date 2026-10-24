"""Worker: claims queued incidents and runs investigations.

Run as many workers as you need; the SKIP LOCKED queue keeps them from colliding.
SIGTERM finishes the current job before exiting, so deploys do not drop work.

    python -m opspilot.worker.main
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.orm import Session, sessionmaker

from common.context import incident_id_var
from common.logging import configure_logging
from opspilot.agent.investigator import Budget, Investigator
from opspilot.agent.prompts import IncidentContext
from opspilot.agent.tools import MCPToolGateway, ToolGateway
from opspilot.db import queue
from opspilot.db.models import Incident
from opspilot.db.session import make_engine, make_session_factory
from opspilot.llm.factory import build_llm
from opspilot.llm.types import LLMProvider
from opspilot.settings import OpsPilotSettings

log = logging.getLogger("opspilot.worker")

GatewayFactory = Callable[[], AbstractAsyncContextManager[ToolGateway]]


def incident_context(incident: Incident) -> IncidentContext:
    return IncidentContext(
        incident_id=str(incident.id),
        service=incident.service,
        alarm_name=incident.alarm_name,
        window_minutes=incident.window_minutes,
        description=incident.description,
        started_at=incident.alarm_time.isoformat() if incident.alarm_time else None,
    )


class Worker:
    def __init__(
        self,
        settings: OpsPilotSettings,
        sessions: sessionmaker[Session],
        llm: LLMProvider,
        gateway_factory: GatewayFactory,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.llm = llm
        self.gateway_factory = gateway_factory
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.budget = Budget(
            max_tool_calls=settings.agent_max_tool_calls,
            max_tokens=settings.agent_max_tokens,
            timeout_s=settings.agent_timeout_s,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
        )
        self._stop = asyncio.Event()

    def stop(self) -> None:
        log.info("shutdown requested; finishing current job", extra={"event": "worker_stopping"})
        self._stop.set()

    async def process_one(self) -> bool:
        """Claim and process at most one job. Returns True if a job was processed."""
        with self.sessions() as session:
            incident = await asyncio.to_thread(
                queue.claim_next, session, self.worker_id, self.settings.worker_lease_s
            )
        if incident is None:
            return False

        token = incident_id_var.set(str(incident.id))
        log.info(
            "investigation started", extra={"event": "job_claimed", "attempt": incident.attempts}
        )
        try:
            async with self.gateway_factory() as tools:
                result = await Investigator(self.llm, tools, self.budget).run(
                    incident_context(incident)
                )
        except Exception as exc:  # infrastructure failure (MCP down, etc.): retry later
            log.exception("investigation crashed", extra={"event": "job_error"})
            with self.sessions() as session:
                outcome = await asyncio.to_thread(
                    queue.fail_or_retry,
                    session,
                    incident.id,
                    self.worker_id,
                    f"{type(exc).__name__}: {exc}",
                )
            log.warning(f"job {outcome}", extra={"event": f"job_{outcome}"})
        else:
            with self.sessions() as session:
                saved = await asyncio.to_thread(
                    queue.complete, session, incident.id, self.worker_id, result
                )
            log.info(
                "investigation stored" if saved else "lease lost; result discarded",
                extra={"event": "job_done" if saved else "job_lease_lost"},
            )
        finally:
            incident_id_var.reset(token)
        return True

    async def run_forever(self) -> None:
        log.info(f"worker {self.worker_id} started", extra={"event": "worker_started"})
        while not self._stop.is_set():
            try:
                worked = await self.process_one()
            except Exception:
                log.exception("worker loop error", extra={"event": "worker_error"})
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self.settings.worker_poll_interval_s)
        log.info("worker stopped", extra={"event": "worker_stopped"})


def main() -> None:
    settings = OpsPilotSettings()
    configure_logging("opspilot-worker", settings.log_level)
    sessions = make_session_factory(make_engine(settings.database_url))
    servers: dict = dict(settings.mcp_servers)
    worker = Worker(
        settings,
        sessions,
        build_llm(settings),
        lambda: MCPToolGateway(servers, timeout_s=settings.tool_timeout_s),
    )

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop)
        await worker.run_forever()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
