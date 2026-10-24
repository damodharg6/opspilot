import json
from dataclasses import asdict
from pathlib import Path
from typing import get_args

from evals.runner import check_baseline, load_scenarios, run_offline
from evals.scoring import summarize
from mcp_servers.backends.local import LocalLogsBackend
from mcp_servers.logs_server import build_logs_server
from opspilot.agent.investigator import Budget, Investigator
from opspilot.agent.prompts import IncidentContext
from opspilot.agent.report import Category
from opspilot.agent.tools import MCPToolGateway, RecordingGateway
from opspilot.llm.testing import RecordingLLM, ScriptedLLM
from tests.test_agent import answer, tool_call
from tests.test_mcp_servers import clock, log_file  # noqa: F401 - fixture reuse


def test_scenarios_file_is_valid() -> None:
    scenarios = load_scenarios()
    ids = [s["id"] for s in scenarios]
    assert len(ids) == len(set(ids)) >= 8
    assert {s["expected"] for s in scenarios} <= set(get_args(Category))
    assert all(s["warmup_s"] >= s["window_minutes"] * 60 for s in scenarios)


async def record_fixture(path: Path, log_file: Path, expected: str) -> None:  # noqa: F811
    ctx = IncidentContext("eval-x", "shop", "shop-health-degraded", 15)
    llm = RecordingLLM(ScriptedLLM([tool_call("search_logs", {"query": "status>=500"}), answer()]))
    budget = Budget()
    async with MCPToolGateway({"logs": build_logs_server(LocalLogsBackend(log_file, clock))}) as g:
        tools = RecordingGateway(g)
        await Investigator(llm, tools, budget).run(ctx)
    fixture = {
        "scenario": {"id": path.stem, "expected": expected},
        "context": asdict(ctx),
        "tool_specs": [asdict(s) for s in tools.specs],
        "tool_calls": tools.calls,
        "llm_responses": [r.to_dict() for r in llm.recorded],
        "budget": asdict(budget),
        "model": "scripted",
    }
    path.write_text(json.dumps(fixture))


async def test_offline_replay_scores_recorded_runs(tmp_path: Path, log_file: Path) -> None:  # noqa: F811
    await record_fixture(tmp_path / "right.json", log_file, expected="bad_deploy")
    await record_fixture(tmp_path / "wrong.json", log_file, expected="memory")
    summary = await run_offline(tmp_path)
    assert summary["n"] == 2 and summary["accuracy"] == 0.5
    assert summary["grounded_rate"] == 1.0


def test_baseline_gate(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    assert check_baseline({"accuracy": 0.5}, baseline) is True  # no baseline: gate skipped
    baseline.write_text(json.dumps({"accuracy": 0.75}))
    assert check_baseline({"accuracy": 0.75}, baseline) is True
    assert check_baseline({"accuracy": 0.625}, baseline) is False


def test_summarize_empty() -> None:
    assert summarize([]) == {"n": 0}
