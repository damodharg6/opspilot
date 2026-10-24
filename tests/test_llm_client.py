import json

import httpx
import pytest

from opspilot.llm.openai_compat import OpenAICompatibleProvider
from opspilot.llm.types import ChatMessage, LLMError

OK_BODY = {
    "id": "x",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "count_errors", "arguments": '{"minutes": 15}'},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
}


def provider(
    responses: list[httpx.Response], seen: list[httpx.Request]
) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return OpenAICompatibleProvider(
        base_url="https://llm.test/v1",
        api_key="k",
        model="test-model",
        max_retries=2,
        backoff_base_s=0.001,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_retries_rate_limit_then_parses_tool_call() -> None:
    seen: list[httpx.Request] = []
    p = provider(
        [
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(200, json=OK_BODY),
        ],
        seen,
    )
    resp = await p.complete([ChatMessage(role="user", content="hi")])
    assert len(seen) == 2
    assert resp.tool_calls[0].name == "count_errors"
    assert resp.tool_calls[0].arguments == {"minutes": 15}
    assert resp.usage.total == 110
    assert json.loads(seen[0].content)["temperature"] == 0.0


async def test_auth_error_is_not_retried() -> None:
    seen: list[httpx.Request] = []
    p = provider([httpx.Response(401, json={"error": {"message": "bad key"}})], seen)
    with pytest.raises(LLMError, match="rejected"):
        await p.complete([ChatMessage(role="user", content="hi")])
    assert len(seen) == 1


async def test_gives_up_after_max_retries() -> None:
    seen: list[httpx.Request] = []
    p = provider([httpx.Response(503, json={"error": {"message": "down"}}) for _ in range(3)], seen)
    with pytest.raises(LLMError, match="unavailable after 3 attempts"):
        await p.complete([ChatMessage(role="user", content="hi")])
    assert len(seen) == 3
