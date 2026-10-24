"""Fault injection: realistic, controllable failures for OpsPilot to investigate.

Each fault leaves a different fingerprint in logs and metrics:

| fault            | fingerprint                                               |
|------------------|-----------------------------------------------------------|
| slow_db          | latency rises on DB routes, few errors                    |
| bad_config       | OperationalError on every DB route, /health fails         |
| memory_leak      | rss_mb climbs steadily in runtime_stats lines             |
| buggy_deploy     | KeyError 500s on POST /orders for some products only      |
| stock_exhaustion | many 409s, no 5xx: a business problem, not an outage      |

Fault state is per process. With several replicas you would keep it in a shared
store; for a single demo container this keeps the code simple.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

FaultName = Literal["slow_db", "bad_config", "memory_leak", "buggy_deploy", "stock_exhaustion"]
FAULT_NAMES: tuple[str, ...] = (
    "slow_db",
    "bad_config",
    "memory_leak",
    "buggy_deploy",
    "stock_exhaustion",
)

DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "slow_db": {"delay_ms": 800},
    "bad_config": {},
    "memory_leak": {"kb_per_request": 512, "max_mb": 400},
    "buggy_deploy": {},
    "stock_exhaustion": {"product_id": 2},
}


@dataclass
class FaultState:
    name: str
    params: dict[str, Any]
    enabled_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FaultRegistry:
    def __init__(self) -> None:
        self._active: dict[str, FaultState] = {}
        self._lock = threading.Lock()
        self._leak: list[bytearray] = []
        self.saved_stock: dict[int, int] = {}

    # --- switching -------------------------------------------------------------
    def enable(self, name: str, params: dict[str, Any] | None = None) -> FaultState:
        if name not in FAULT_NAMES:
            raise KeyError(name)
        merged = {**DEFAULT_PARAMS[name], **(params or {})}
        state = FaultState(name=name, params=merged)
        with self._lock:
            self._active[name] = state
        return state

    def disable(self, name: str) -> FaultState | None:
        with self._lock:
            state = self._active.pop(name, None)
            if name == "memory_leak":
                self._leak.clear()
        return state

    def disable_all(self) -> None:
        for name in list(self._active):
            self.disable(name)

    def active(self) -> dict[str, FaultState]:
        with self._lock:
            return dict(self._active)

    def is_on(self, name: str) -> bool:
        return name in self._active

    def param(self, name: str, key: str) -> Any:
        state = self._active.get(name)
        return state.params[key] if state else DEFAULT_PARAMS[name][key]

    # --- hooks called by the app -------------------------------------------------
    def on_request(self) -> None:
        """memory_leak: retain a chunk of memory on every request, up to a cap."""
        if not self.is_on("memory_leak"):
            return
        kb = int(self.param("memory_leak", "kb_per_request"))
        cap_mb = float(self.param("memory_leak", "max_mb"))
        if self.leak_mb() < cap_mb:
            with self._lock:
                self._leak.append(bytearray(kb * 1024))

    def leak_mb(self) -> float:
        return round(sum(len(chunk) for chunk in self._leak) / 1_048_576, 1)

    def db_delay_s(self) -> float:
        if not self.is_on("slow_db"):
            return 0.0
        return float(self.param("slow_db", "delay_ms")) / 1000
