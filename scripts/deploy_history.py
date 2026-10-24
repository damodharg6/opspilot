"""A small simulated deploy history for the git MCP server to read.

The demo needs a realistic chain of evidence: "errors started right after release
v1.5.0, which changed pricing". This script keeps a separate git repo (default
var/deploy-history) with a few ordinary releases, and records a release commit
when the buggy_deploy fault is switched on (and a rollback when switched off).

It is clearly a simulation: the real application code does not change.

    python -m scripts.deploy_history init
"""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(os.getenv("DEPLOY_HISTORY_PATH", "var/deploy-history"))
AUTHOR = ("Release Bot", "release-bot@shop.local")

PRICING_V1 = """def calculate_total(price, quantity):
    return round(price * quantity, 2)
"""

PRICING_V2 = """PROMOTION_RATES = {1: 0.05, 3: 0.10}


def calculate_total(product_id, price, quantity):
    discount = PROMOTION_RATES[product_id]
    subtotal = price * quantity
    return round(subtotal - subtotal * discount, 2)
"""


def _git(*args: str, when: datetime | None = None) -> str:
    env = {**os.environ}
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    env.update(
        GIT_AUTHOR_NAME=AUTHOR[0],
        GIT_AUTHOR_EMAIL=AUTHOR[1],
        GIT_COMMITTER_NAME=AUTHOR[0],
        GIT_COMMITTER_EMAIL=AUTHOR[1],
    )
    result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        ["git", "-C", str(REPO), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def _commit(files: dict[str, str], message: str, when: datetime | None = None) -> None:
    for rel, content in files.items():
        path = REPO / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _git("add", rel)
    _git("commit", "-q", "-m", message, when=when)


def init(force: bool = False) -> None:
    if (REPO / ".git").exists() and not force:
        print(f"{REPO} already exists")
        return
    REPO.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main")
    now = datetime.now(UTC)
    _commit(
        {"shop/pricing.py": PRICING_V1, "VERSION": "1.3.0\n"},
        "release v1.3.0: order pricing service",
        now - timedelta(days=6),
    )
    _commit(
        {"shop/products.py": "PAGE_SIZE = 50\n", "VERSION": "1.3.1\n"},
        "release v1.3.1: paginate product listing",
        now - timedelta(days=3),
    )
    _commit(
        {"shop/health.py": "CHECK_DB = True\n", "VERSION": "1.4.0\n"},
        "release v1.4.0: health check verifies database",
        now - timedelta(hours=20),
    )
    print(f"initialised demo deploy history in {REPO}")


def record_release() -> None:
    if not (REPO / ".git").exists():
        return
    _commit(
        {"shop/pricing.py": PRICING_V2, "VERSION": "1.5.0\n"},
        "release v1.5.0: apply promotion discounts to order totals",
    )


def record_rollback() -> None:
    if not (REPO / ".git").exists():
        return
    _commit(
        {"shop/pricing.py": PRICING_V1, "VERSION": "1.5.1\n"},
        "release v1.5.1: roll back promotion pricing",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["init", "reinit"])
    args = parser.parse_args()
    if args.cmd == "reinit":
        import shutil

        shutil.rmtree(REPO, ignore_errors=True)
    init()


if __name__ == "__main__":
    main()
