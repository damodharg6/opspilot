import asyncio
import json
from pathlib import Path

import pytest

from mcp_servers.backends.local import LocalLogsBackend, LocalMetricsBackend
from mcp_servers.logs_server import build_logs_server
from mcp_servers.metrics_server import build_metrics_server
from opspilot.agent.grounding import ground_report
from opspilot.agent.investigator import Budget, Investigator, parse_report
from opspilot.agent.prompts import IncidentContext
from opspilot.agent.report import Evidence, IncidentReport
from opspilot.agent.tools import MCPToolGateway, RecordingGateway, ReplayGateway
from opspilot.llm.openai_compat import _parse_arguments, to_openai_messages
from opspilot.llm.testing import ScriptedLLM
from opspilot.llm.types import ChatMessage, LLMResponse, ToolCall, Usage
from tests.test_mcp_servers import clock, log_file  # noqa: F401 - fixture reuse

CTX = IncidentContext(
    incident_id="inc-1", service="shop", alarm_name="shop-5xx-high", window_minutes=15
)


def tool_call(name: str, args: dict, cid: str = "c1") -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[ToolCall(cid, name, args)], usage=Usage(500, 20))


def answer(**overrides: object) -> LLMResponse:
    report = {
        "summary": "POST /orders fails with KeyError",
        "root_cause_category": "bad_deploy",
        "root_cause": "Pricing code raises KeyError for products without a promotion.",
        "evidence": [{"tool": "search_logs", "snippet": "POST /orders 500"}],
        "confidence": "high",
        "suggested_fix": "Roll back the latest release.",
        **overrides,
    }
    return LLMResponse(content=json.dumps(report), usage=Usage(800, 120))


@pytest.fixture()
def gateway_servers(log_file: Path) -> dict:  # noqa: F811
    return {
        "logs": build_logs_server(LocalLogsBackend(log_file, clock)),
        "metrics": build_metrics_server(LocalMetricsBackend(log_file, clock)),
    }


async def run(llm: ScriptedLLM, servers: dict, budget: Budget | None = None):
    async with MCPToolGateway(servers) as tools:
        return await Investigator(llm, tools, budget).run(CTX)


async def test_happy_path_grounded_report(gateway_servers: dict) -> None:
    llm = ScriptedLLM(
        [
            tool_call("count_errors", {"group_by": "error_type"}),
            tool_call("search_logs", {"query": "status>=500"}, "c2"),
            answer(),
        ]
    )
    result = await run(llm, gateway_servers)
    assert result.status == "completed" and result.stop_reason == "answered"
    assert [s.tool for s in result.steps] == ["count_errors", "search_logs"]
    assert result.report.root_cause_category == "bad_deploy"
    assert result.report.confidence == "high" and len(result.report.evidence) == 1
    assert result.usage.total == 500 + 20 + 500 + 20 + 800 + 120
    # tool output reached the model wrapped as untrusted data
    tool_msgs = [m for m in llm.calls[-1][0] if m.role == "tool"]
    assert tool_msgs[0].content.startswith("<tool_output>")


async def test_hallucinated_evidence_is_dropped(gateway_servers: dict) -> None:
    fake = [{"tool": "search_logs", "snippet": "OutOfMemoryError in worker 7"}]
    llm = ScriptedLLM([tool_call("search_logs", {"query": "status>=500"}), answer(evidence=fake)])
    result = await run(llm, gateway_servers)
    assert result.report.evidence == []
    assert result.report.confidence == "low"
    assert result.evidence_dropped == 1


async def test_tool_budget_forces_final_answer(gateway_servers: dict) -> None:
    llm = ScriptedLLM(
        [
            tool_call("count_errors", {}),
            tool_call("count_errors", {}, "c2"),  # over budget: not executed
            answer(),
        ]
    )
    result = await run(llm, gateway_servers, Budget(max_tool_calls=1))
    assert len(result.steps) == 1
    assert result.status == "partial" and result.stop_reason == "tool_budget"
    assert llm.calls[-1][1] is None  # final call made without tools


async def test_invalid_json_gets_one_repair(gateway_servers: dict) -> None:
    llm = ScriptedLLM(
        [
            tool_call("search_logs", {"query": "status>=500"}),
            LLMResponse(content="I think it's a bug"),
            answer(),
        ]
    )
    result = await run(llm, gateway_servers)
    assert result.status == "completed"
    assert "not a valid report" in llm.calls[-1][0][-1].content


async def test_invalid_twice_fails_cleanly(gateway_servers: dict) -> None:
    llm = ScriptedLLM([LLMResponse(content="nope"), LLMResponse(content="still nope")])
    result = await run(llm, gateway_servers)
    assert result.status == "failed" and result.report.root_cause_category == "unknown"
    assert result.error


async def test_tool_error_is_returned_to_model(gateway_servers: dict) -> None:
    llm = ScriptedLLM(
        [tool_call("search_logs", {"query": "hostname=x"}), tool_call("nope", {}, "c2"), answer()]
    )
    result = await run(llm, gateway_servers)
    assert [s.is_error for s in result.steps] == [True, True]
    assert result.status == "completed"


async def test_timeout_returns_partial(gateway_servers: dict) -> None:
    class SlowLLM(ScriptedLLM):
        async def complete(self, *args, **kwargs):
            await asyncio.sleep(5)

    result = await run(SlowLLM([]), gateway_servers, Budget(timeout_s=0.2))
    assert result.status == "partial" and result.stop_reason == "timeout"


async def test_record_and_replay_reproduce_the_run(gateway_servers: dict) -> None:
    script = [tool_call("count_errors", {"group_by": "path"}), answer()]
    async with MCPToolGateway(gateway_servers) as live:
        recorder = RecordingGateway(live)
        first = await Investigator(ScriptedLLM(list(script)), recorder).run(CTX)
    specs = [vars(s) for s in recorder.specs]
    replay = ReplayGateway(specs, recorder.calls)
    second = await Investigator(ScriptedLLM(list(script)), replay).run(CTX)
    assert second.report == first.report
    assert [s.result for s in second.steps] == [s.result for s in first.steps]


# --- pure functions --------------------------------------------------------------------------
def test_parse_report_tolerates_code_fences() -> None:
    content = "```json\n" + answer().content + "\n```"
    assert parse_report(content).root_cause_category == "bad_deploy"


def test_grounding_lowers_confidence_by_one_level() -> None:
    report = IncidentReport(
        summary="s",
        root_cause_category="db_latency",
        root_cause="r",
        evidence=[Evidence(tool="t", snippet="real line"), Evidence(tool="t", snippet="made up")],
        confidence="high",
        suggested_fix="f",
    )
    result = ground_report(report, ['{"message": "a  real   line here"}'])
    assert result.kept == 1 and result.dropped == 1
    assert result.report.confidence == "medium"


def test_openai_message_conversion_and_bad_arguments() -> None:
    msgs = [
        ChatMessage(
            role="assistant", content=None, tool_calls=[ToolCall("x", "search_logs", {"q": 1})]
        ),
        ChatMessage(role="tool", content="out", tool_call_id="x"),
    ]
    out = to_openai_messages(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"q": 1}'
    assert out[1]["tool_call_id"] == "x"
    assert "__invalid_json__" in _parse_arguments("{not json")
