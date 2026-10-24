import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "alarm_forwarder", Path(__file__).parents[1] / "infra/lambda/alarm_forwarder.py"
)
forwarder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forwarder)


def sns_event(state: str) -> dict:
    message = {
        "AlarmName": "opspilot-shop-5xx-high",
        "NewStateValue": state,
        "NewStateReason": "Threshold Crossed: 1 datapoint [12.0] was greater than 5.0",
        "StateChangeTime": "2026-10-24T07:30:00.000+0000",
    }
    return {"Records": [{"Sns": {"Message": json.dumps(message)}}]}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSPILOT_URL", "http://10.0.1.5:8080/")
    monkeypatch.setenv("OPSPILOT_API_SECRET", "s3cret")


class FakeResponse(io.BytesIO):
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_forwards_alarm_with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []

    def fake_urlopen(request, timeout):
        sent.append(request)
        return FakeResponse(b"{}")

    monkeypatch.setattr(forwarder.urllib.request, "urlopen", fake_urlopen)
    assert forwarder.handler(sns_event("ALARM"), None) == {"forwarded": 1, "skipped": 0}
    req = sent[0]
    assert req.full_url == "http://10.0.1.5:8080/incidents"
    assert req.get_header("X-opspilot-secret") == "s3cret"
    body = json.loads(req.data)
    assert body["alarm_name"] == "opspilot-shop-5xx-high" and body["source"] == "cloudwatch"


def test_ok_transition_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forwarder.urllib.request, "urlopen", lambda *a, **k: pytest.fail("called"))
    assert forwarder.handler(sns_event("OK"), None) == {"forwarded": 0, "skipped": 1}


def test_server_error_is_raised_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "down", {}, None)

    monkeypatch.setattr(forwarder.urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError):
        forwarder.handler(sns_event("ALARM"), None)
