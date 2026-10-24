"""Generate realistic traffic against the shop and print a latency/status summary.

python -m scripts.load --rate 8 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import time
from collections import Counter

import httpx


async def one_request(client: httpx.AsyncClient, order_ids: list[int]) -> tuple[int, float]:
    roll = random.random()
    started = time.perf_counter()
    try:
        if roll < 0.45:
            r = await client.get("/products")
        elif roll < 0.85:
            body = {"product_id": random.randint(1, 4), "quantity": random.randint(1, 3)}
            r = await client.post("/orders", json=body)
            if r.status_code == 201:
                order_ids.append(r.json()["id"])
        elif order_ids:
            r = await client.get(f"/orders/{random.choice(order_ids[-50:])}")
        else:
            r = await client.get("/orders", params={"limit": 10})
        status = r.status_code
    except httpx.HTTPError:
        status = 0  # connection error / timeout
    return status, (time.perf_counter() - started) * 1000


async def run(base_url: str, rate: float, duration: float) -> dict[str, object]:
    statuses: Counter[int] = Counter()
    latencies: list[float] = []
    order_ids: list[int] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        tasks: list[asyncio.Task[tuple[int, float]]] = []
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            tasks.append(asyncio.create_task(one_request(client, order_ids)))
            await asyncio.sleep(random.expovariate(rate))  # Poisson arrivals, like real users
        results = await asyncio.gather(*tasks)
    for status, ms in results:
        statuses[status] += 1
        latencies.append(ms)
    return summarize(statuses, latencies)


def summarize(statuses: Counter[int], latencies: list[float]) -> dict[str, object]:
    if not latencies:
        return {"requests": 0}
    q = statistics.quantiles(latencies, n=100) if len(latencies) > 1 else latencies * 99
    return {
        "requests": sum(statuses.values()),
        "status_counts": dict(sorted(statuses.items())),
        "p50_ms": round(q[49], 1),
        "p95_ms": round(q[94], 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("SHOP_URL", "http://localhost:8000"))
    parser.add_argument("--rate", type=float, default=8.0, help="requests per second")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds")
    args = parser.parse_args()
    print(asyncio.run(run(args.url, args.rate, args.duration)))


if __name__ == "__main__":
    main()
