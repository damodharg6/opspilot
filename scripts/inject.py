"""Turn shop faults on and off.

    python -m scripts.inject list
    python -m scripts.inject on slow_db --param delay_ms=1200
    python -m scripts.inject off slow_db
    python -m scripts.inject reset

Turning buggy_deploy on/off also records a release/rollback commit in the demo
deploy history (see scripts/deploy_history.py), so the git tools see a "deploy".
"""

from __future__ import annotations

import argparse
import json

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from scripts import deploy_history


class ScriptSettings(BaseSettings):
    """Reads SHOP_URL / SHOP_ADMIN_TOKEN from the environment or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    shop_url: str = "http://localhost:8000"
    shop_admin_token: SecretStr = SecretStr("")


def _client() -> httpx.Client:
    settings = ScriptSettings()
    token = settings.shop_admin_token.get_secret_value()
    if not token:
        raise SystemExit("Set SHOP_ADMIN_TOKEN (see .env.example)")
    return httpx.Client(base_url=settings.shop_url, headers={"X-Admin-Token": token}, timeout=10)


def _parse_params(pairs: list[str]) -> dict[str, object]:
    params: dict[str, object] = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


def enable(name: str, params: dict[str, object] | None = None) -> dict[str, object]:
    with _client() as c:
        r = c.post(f"/admin/faults/{name}", json={"params": params or {}})
        r.raise_for_status()
    if name == "buggy_deploy":
        deploy_history.record_release()
    return r.json()


def disable(name: str) -> dict[str, object]:
    with _client() as c:
        r = c.delete(f"/admin/faults/{name}")
        r.raise_for_status()
    if name == "buggy_deploy" and r.json().get("was_active"):
        deploy_history.record_rollback()
    return r.json()


def reset() -> None:
    with _client() as c:
        active = c.get("/admin/faults").json()["active"]
    for name in active:
        disable(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("reset")
    on = sub.add_parser("on")
    on.add_argument("name")
    on.add_argument("--param", action="append", default=[], help="key=value, repeatable")
    off = sub.add_parser("off")
    off.add_argument("name")
    args = parser.parse_args()

    if args.cmd == "list":
        with _client() as c:
            print(json.dumps(c.get("/admin/faults").json(), indent=2))
    elif args.cmd == "on":
        print(enable(args.name, _parse_params(args.param)))
    elif args.cmd == "off":
        print(disable(args.name))
    else:
        reset()
        print("all faults disabled")


if __name__ == "__main__":
    main()
