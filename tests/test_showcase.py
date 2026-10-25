"""Showcase: live-run server, demo reset helpers and the recorded data the site replays."""

import asyncio
import json
import subprocess
import time
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts import deploy_history, record_demos
from scripts.demo import LIVE_WARMUP_S, SCENARIO_IDS, reset_deploy_history, scenario
from scripts.showcase_server import create_app

SITE_DIR = Path(__file__).resolve().parent.parent / "showcase"
DATA_DIR = SITE_DIR / "data"
PAGES = sorted(SITE_DIR.rglob("*.html"))


def fake_record(scenario_id: str) -> dict[str, Any]:
    return {"id": scenario_id, "title": "t", "tagline": "g", "correct": True}


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<h1>showcase</h1>")
    return tmp_path


def client_for(runner: Callable[..., Any], site: Path) -> TestClient:
    return TestClient(create_app(runner=runner, site_dir=site), base_url="http://localhost")


def wait_until_finished(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(100):
        run = client.get(f"/live/runs/{run_id}").json()
        if run["state"] != "running":
            return run
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_live_run_reports_progress_and_record(site: Path) -> None:
    async def runner(scenario_id: str, *, live: bool, progress: Callable[[str, str], None]) -> dict:
        assert live
        progress("warmup", "traffic")
        return fake_record(scenario_id)

    with client_for(runner, site) as client:
        started = client.post("/live/runs", json={"scenario": "bad_deploy_basic"})
        assert started.status_code == 202
        run = wait_until_finished(client, started.json()["id"])

    assert run["state"] == "done"
    assert run["record"]["id"] == "bad_deploy_basic"
    assert [e["phase"] for e in run["events"]] == ["warmup"]


def test_only_one_live_run_at_a_time(site: Path) -> None:
    release = asyncio.Event()

    async def runner(scenario_id: str, **_: Any) -> dict:
        await release.wait()
        return fake_record(scenario_id)

    with client_for(runner, site) as client:
        assert client.post("/live/runs", json={"scenario": "bad_deploy_basic"}).status_code == 202
        busy = client.post("/live/runs", json={"scenario": "db_latency_basic"})
        assert busy.status_code == 409
        assert client.get("/live/status").json()["running"] is not None


def test_runner_failure_is_reported_not_raised(site: Path) -> None:
    async def runner(scenario_id: str, **_: Any) -> dict:
        raise ConnectionError("api down")

    with client_for(runner, site) as client:
        run_id = client.post("/live/runs", json={"scenario": "bad_deploy_basic"}).json()["id"]
        run = wait_until_finished(client, run_id)
    assert run["state"] == "error"
    assert run["error"] == "ConnectionError: api down"


def test_unknown_scenario_and_run_are_404(site: Path) -> None:
    with client_for(lambda *a, **k: None, site) as client:
        assert client.post("/live/runs", json={"scenario": "drop_tables"}).status_code == 404
        assert client.get("/live/runs/nope").status_code == 404


def test_serves_site_and_rejects_foreign_host_header(site: Path) -> None:
    app = create_app(runner=lambda *a, **k: None, site_dir=site)
    assert "showcase" in TestClient(app, base_url="http://localhost").get("/").text
    # A DNS-rebinding page would arrive with its own Host header.
    assert TestClient(app, base_url="http://evil.example").get("/live/status").status_code == 400


def test_live_runs_use_a_shorter_warmup_only() -> None:
    recorded, live = scenario("memory_leak_basic"), scenario("memory_leak_basic", live=True)
    assert live["warmup_s"] == LIVE_WARMUP_S < recorded["warmup_s"]
    assert live["fault_s"] == recorded["fault_s"]
    with pytest.raises(KeyError):
        scenario("healthy_control")


def test_reset_deploy_history_starts_fresh(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "history"
    monkeypatch.setattr(deploy_history, "REPO", repo)
    reset_deploy_history()
    deploy_history.record_release()
    (repo / "stray.txt").write_text("left over")

    reset_deploy_history()

    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert len(log) == 3
    assert not any("v1.5.0" in line for line in log)
    assert not (repo / "stray.txt").exists()


def test_write_index_lists_recorded_demos_in_order(tmp_path: Path) -> None:
    for sid in reversed(SCENARIO_IDS[:2]):
        (tmp_path / f"{sid}.json").write_text(json.dumps({**fake_record(sid), "trace": []}))
    entries = record_demos.write_index(tmp_path)
    assert [e["id"] for e in entries] == list(SCENARIO_IDS[:2])
    assert json.loads((tmp_path / "index.json").read_text())["demos"] == entries


@pytest.mark.skipif(not (DATA_DIR / "index.json").exists(), reason="no recorded demos yet")
def test_recorded_demos_have_what_the_site_needs() -> None:
    index = json.loads((DATA_DIR / "index.json").read_text())["demos"]
    assert index, "index.json lists no demos"
    for entry in index:
        record = json.loads((DATA_DIR / f"{entry['id']}.json").read_text())
        assert record["id"] in SCENARIO_IDS
        assert record["incident"]["status"] in ("done", "failed")
        assert record["traffic"]["fault"]["requests"] > 0
        for step in record["trace"]:
            assert {"step", "tool", "arguments", "result_preview", "duration_ms"} <= set(step)
        if report := record["incident"]["report"]:
            assert record["correct"] == (report["category"] == record["expected"])


class _PageScan(HTMLParser):
    """Collects local links/assets, inline script bodies and style attributes of one page."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.inline_scripts = 0
        self.style_attrs = 0
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.style_attrs += "style" in values
        self._in_script = tag == "script" and "src" not in values
        for key in ("href", "src"):
            if (ref := values.get(key)) and not ref.startswith(("http", "mailto:", "#")):
                self.refs.append(ref)

    def handle_endtag(self, tag: str) -> None:
        self._in_script = False

    def handle_data(self, data: str) -> None:
        self.inline_scripts += self._in_script and bool(data.strip())


def scan(page: Path) -> _PageScan:
    parser = _PageScan()
    parser.feed(page.read_text())
    return parser


def resolve(page: Path, ref: str) -> Path:
    target = (page.parent / ref.split("#")[0].split("?")[0]).resolve()
    return target / "index.html" if ref.split("#")[0].endswith("/") or target.is_dir() else target


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(SITE_DIR)))
def test_site_links_and_assets_resolve(page: Path) -> None:
    missing = [ref for ref in scan(page).refs if not resolve(page, ref).exists()]
    assert not missing, f"broken links in {page.name}: {missing}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(SITE_DIR)))
def test_site_has_no_inline_code_blocked_by_csp(page: Path) -> None:
    found = scan(page)
    assert (found.inline_scripts, found.style_attrs) == (0, 0)


def test_blog_lists_every_post() -> None:
    posts = {p.parent.name for p in (SITE_DIR / "blog").glob("*/index.html")}
    refs = scan(SITE_DIR / "blog" / "index.html").refs
    listed = {ref.strip("/") for ref in refs if ref.endswith("/")}
    assert posts and posts <= listed


def test_hidden_attribute_beats_component_display_rules() -> None:
    # Without this rule `.btn { display: inline-flex }` showed the hidden Run live button.
    css = (SITE_DIR / "assets" / "styles.css").read_text()
    assert "[hidden] { display: none !important; }" in css
