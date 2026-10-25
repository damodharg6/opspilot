"""Record the showcase demos on the running stack (make up first).

    python -m scripts.record_demos                       # all four scenarios
    python -m scripts.record_demos --only db_latency_basic

Writes showcase/data/<scenario>.json and showcase/data/index.json. These real runs
are what the public showcase site replays.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from scripts.demo import SCENARIO_IDS, run_demo

DATA_DIR = Path(__file__).resolve().parent.parent / "showcase" / "data"
PAUSE_BETWEEN_S = 60  # free LLM tiers limit requests per minute


def write_index(data_dir: Path = DATA_DIR) -> list[dict[str, Any]]:
    entries = []
    for scenario_id in SCENARIO_IDS:
        path = data_dir / f"{scenario_id}.json"
        if path.exists():
            record = json.loads(path.read_text())
            entries.append({k: record[k] for k in ("id", "title", "tagline", "correct")})
    (data_dir / "index.json").write_text(json.dumps({"demos": entries}, indent=2) + "\n")
    return entries


async def record(ids: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for i, scenario_id in enumerate(ids):
        if i:
            await asyncio.sleep(PAUSE_BETWEEN_S)

        def show(phase: str, detail: str, sid: str = scenario_id) -> None:
            print(f"[{sid}] {phase}: {detail}", flush=True)

        record_ = await run_demo(scenario_id, progress=show)
        (DATA_DIR / f"{scenario_id}.json").write_text(json.dumps(record_, indent=2) + "\n")
    write_index()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", choices=SCENARIO_IDS, help="scenario ids")
    args = parser.parse_args()
    asyncio.run(record(args.only or list(SCENARIO_IDS)))


if __name__ == "__main__":
    main()
