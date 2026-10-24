"""Grounding check: every evidence snippet must appear in real tool output.

This is the main defence against a hallucinated report. Ungrounded evidence is
dropped and confidence is lowered, so a report never claims more than the tools showed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from opspilot.agent.report import CONFIDENCE_ORDER, Evidence, IncidentReport

_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().strip("\"'").lower()


def _variants(output: str) -> list[str]:
    """Tool output is JSON text; also compare against its decoded string values."""
    variants = [_normalize(output)]
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return variants
    variants.append(_normalize(json.dumps(data, separators=(", ", ": "))))
    return variants


@dataclass
class GroundingResult:
    report: IncidentReport
    kept: int
    dropped: int


def ground_report(report: IncidentReport, tool_outputs: list[str]) -> GroundingResult:
    haystacks = [v for output in tool_outputs for v in _variants(output)]
    kept: list[Evidence] = []
    for ev in report.evidence:
        needle = _normalize(ev.snippet)
        if needle and any(needle in h for h in haystacks):
            kept.append(ev)
    dropped = len(report.evidence) - len(kept)

    confidence = report.confidence
    if dropped:
        confidence = CONFIDENCE_ORDER[max(0, CONFIDENCE_ORDER.index(confidence) - 1)]
    if not kept and report.root_cause_category != "unknown":
        confidence = "low"
    grounded = report.model_copy(update={"evidence": kept, "confidence": confidence})
    return GroundingResult(report=grounded, kept=len(kept), dropped=dropped)
