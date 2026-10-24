"""Eval runner.

    python -m evals.runner live      # real faults, real tools, real LLM; records fixtures
    python -m evals.runner offline   # replays fixtures: deterministic, no network, CI gate
    python -m evals.runner offline --update-baseline

Live mode runs the Investigator in this process (against the running MCP servers
and the configured LLM) so every tool call and model response can be recorded.
Results are never edited by hand; the baseline only changes with --update-baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from evals.scoring import ScenarioScore, format_table, score, summarize
from opspilot.agent.investigator import Budget, Investigator
from opspilot.agent.prompts import IncidentContext
from opspilot.agent.tools import MCPToolGateway, RecordingGateway, ReplayGateway
from opspilot.llm.factory import build_llm
from opspilot.llm.testing import RecordingLLM, ReplayLLM
from opspilot.llm.types import LLMResponse
from opspilot.settings import OpsPilotSettings

EVALS_DIR = Path(__file__).parent
SCENARIOS = EVALS_DIR / "scenarios.yaml"
FIXTURES = EVALS_DIR / "fixtures"
RESULTS = EVALS_DIR / "results"
BASELINE = EVALS_DIR / "baseline.json"


def load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    defaults = data.get("defaults", {})
    return [{**defaults, **s} for s in data["scenarios"]]


def context_for(scenario: dict[str, Any]) -> IncidentContext:
    return IncidentContext(
        incident_id=f"eval-{scenario['id']}",
        service="shop",
        alarm_name="shop-health-degraded",
        window_minutes=int(scenario["window_minutes"]),
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


# --- live mode ---------------------------------------------------------------------------
async def _noise(base_url: str, seconds: float) -> None:
    import httpx

    async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await client.get("/orders/99999999")
            await asyncio.sleep(0.05)


async def run_live_scenario(
    scenario: dict[str, Any], settings: OpsPilotSettings, shop_url: str
) -> tuple[ScenarioScore, dict[str, Any]]:
    from scripts import inject, load

    inject.reset()
    print(f"[{scenario['id']}] warmup {scenario['warmup_s']}s", flush=True)
    await load.run(shop_url, float(scenario["rate"]), float(scenario["warmup_s"]))
    if scenario.get("fault"):
        inject.enable(scenario["fault"], scenario.get("params"))
    print(
        f"[{scenario['id']}] fault={scenario.get('fault')} for {scenario['fault_s']}s", flush=True
    )
    jobs = [load.run(shop_url, float(scenario["rate"]), float(scenario["fault_s"]))]
    if scenario.get("noise") == "not_found_spike":
        jobs.append(_noise(shop_url, 20))
    await asyncio.gather(*jobs)

    ctx = context_for(scenario)
    llm = RecordingLLM(build_llm(settings))
    budget = Budget(
        max_tool_calls=settings.agent_max_tool_calls,
        max_tokens=settings.agent_max_tokens,
        timeout_s=settings.agent_timeout_s,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    async with MCPToolGateway(dict(settings.mcp_servers), settings.tool_timeout_s) as live_tools:
        tools = RecordingGateway(live_tools)
        result = await Investigator(llm, tools, budget).run(ctx)
    inject.reset()

    fixture = {
        "scenario": scenario,
        "context": asdict(ctx),
        "tool_specs": [asdict(s) for s in tools.specs],
        "tool_calls": tools.calls,
        "llm_responses": [r.to_dict() for r in llm.recorded],
        "budget": asdict(budget),
        "model": llm.model,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return score(scenario["id"], scenario["expected"], result), fixture


async def run_live(only: list[str] | None) -> dict[str, Any]:
    settings = OpsPilotSettings()
    shop_url = os.getenv("SHOP_URL", "http://localhost:8000")
    scores: list[ScenarioScore] = []
    FIXTURES.mkdir(exist_ok=True)
    for scenario in load_scenarios():
        if only and scenario["id"] not in only:
            continue
        s, fixture = await run_live_scenario(scenario, settings, shop_url)
        (FIXTURES / f"{scenario['id']}.json").write_text(json.dumps(fixture, indent=2))
        print(f"[{s.scenario_id}] expected={s.expected} predicted={s.predicted}", flush=True)
        scores.append(s)
    summary = summarize(scores)
    summary["model"] = settings.llm_model
    return summary


# --- offline mode ---------------------------------------------------------------------------
async def replay_fixture(fixture: dict[str, Any]) -> ScenarioScore:
    llm = ReplayLLM([LLMResponse.from_dict(r) for r in fixture["llm_responses"]], fixture["model"])
    tools = ReplayGateway(fixture["tool_specs"], fixture["tool_calls"])
    budget = Budget(**{**fixture["budget"], "timeout_s": 60.0})
    ctx = IncidentContext(**fixture["context"])
    result = await Investigator(llm, tools, budget).run(ctx)
    scenario = fixture["scenario"]
    return score(scenario["id"], scenario["expected"], result)


async def run_offline(fixtures_dir: Path = FIXTURES) -> dict[str, Any]:
    files = sorted(fixtures_dir.glob("*.json"))
    scores = [await replay_fixture(json.loads(f.read_text())) for f in files]
    summary = summarize(scores)
    summary["model"] = json.loads(files[0].read_text())["model"] if files else None
    return summary


def check_baseline(summary: dict[str, Any], baseline_path: Path = BASELINE) -> bool:
    if not baseline_path.exists():
        print("No baseline yet: gate skipped. Create one with --update-baseline.")
        return True
    baseline = json.loads(baseline_path.read_text())
    ok = summary["accuracy"] >= baseline["accuracy"]
    verdict = "PASS" if ok else "FAIL"
    print(f"Baseline gate {verdict}: {summary['accuracy']} vs baseline {baseline['accuracy']}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["live", "offline"])
    parser.add_argument("--only", nargs="*", help="scenario ids to run (live mode)")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    if args.mode == "offline" and not any(FIXTURES.glob("*.json")):
        print("No recorded fixtures yet. Run `python -m evals.runner live` first.")
        sys.exit(0)

    summary = asyncio.run(run_live(args.only) if args.mode == "live" else run_offline())
    print(format_table(summary))

    if args.mode == "live":
        RESULTS.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        model = str(summary.get("model", "model")).replace("/", "_")
        out = RESULTS / f"{stamp}_{model}.json"
        out.write_text(json.dumps(summary, indent=2))
        print(f"results written to {out}")

    if args.update_baseline:
        BASELINE.write_text(
            json.dumps(
                {
                    "accuracy": summary["accuracy"],
                    "n": summary["n"],
                    "model": summary.get("model"),
                    "mode": args.mode,
                    "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
                indent=2,
            )
        )
        print(f"baseline updated: accuracy={summary['accuracy']}")
    elif args.mode == "offline" and not check_baseline(summary):
        sys.exit(1)


if __name__ == "__main__":
    main()
