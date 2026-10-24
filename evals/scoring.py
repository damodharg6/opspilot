"""Turn investigation results into numbers. Pure functions: easy to test, easy to trust."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from opspilot.agent.investigator import InvestigationResult


@dataclass
class ScenarioScore:
    scenario_id: str
    expected: str
    predicted: str
    correct: bool
    grounded: bool
    confidence: str
    status: str
    tool_calls: int
    tokens: int
    latency_s: float
    evidence_dropped: int


def score(scenario_id: str, expected: str, result: InvestigationResult) -> ScenarioScore:
    report = result.report
    has_evidence = bool(report.evidence)
    grounded = result.evidence_dropped == 0 and (
        has_evidence or report.root_cause_category == "unknown"
    )
    return ScenarioScore(
        scenario_id=scenario_id,
        expected=expected,
        predicted=report.root_cause_category,
        correct=report.root_cause_category == expected,
        grounded=grounded,
        confidence=report.confidence,
        status=result.status,
        tool_calls=len(result.steps),
        tokens=result.usage.total,
        latency_s=result.latency_s,
        evidence_dropped=result.evidence_dropped,
    )


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def summarize(scores: list[ScenarioScore]) -> dict[str, Any]:
    if not scores:
        return {"n": 0}
    n = len(scores)
    business = [s for s in scores if s.expected == "business"]
    return {
        "n": n,
        "accuracy": round(sum(s.correct for s in scores) / n, 3),
        "grounded_rate": round(sum(s.grounded for s in scores) / n, 3),
        "business_correct": all(s.correct for s in business) if business else None,
        "mean_latency_s": round(sum(s.latency_s for s in scores) / n, 2),
        "p95_latency_s": round(_p95([s.latency_s for s in scores]), 2),
        "mean_tool_calls": round(sum(s.tool_calls for s in scores) / n, 2),
        "mean_tokens": round(sum(s.tokens for s in scores) / n),
        "scenarios": [asdict(s) for s in scores],
    }


def format_table(summary: dict[str, Any]) -> str:
    rows = [
        "scenario                      expected       predicted      ok  grounded  calls  tokens"
    ]
    for s in summary.get("scenarios", []):
        rows.append(
            f"{s['scenario_id']:<30}{s['expected']:<15}{s['predicted']:<15}"
            f"{'Y' if s['correct'] else 'N':<4}{'Y' if s['grounded'] else 'N':<10}"
            f"{s['tool_calls']:<7}{s['tokens']}"
        )
    rows.append(
        f"\naccuracy={summary['accuracy']}  grounded={summary['grounded_rate']}  "
        f"mean_latency={summary['mean_latency_s']}s  mean_tokens={summary['mean_tokens']}"
    )
    return "\n".join(rows)
