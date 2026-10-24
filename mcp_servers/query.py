"""A tiny, safe filter language for log search.

    status>=500 path=/orders error_type=KeyError "connection refused"

- `field<op>value` conditions on a whitelist of fields (ops: = != >= <= > <)
- anything else is a case-insensitive text term matched against the message

Keeping the language small and whitelisted means it can be translated into a
CloudWatch Logs Insights query without passing raw model text into the query.
"""

from __future__ import annotations

import operator
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ALLOWED_FIELDS = {
    "status",
    "path",
    "method",
    "level",
    "error_type",
    "service",
    "request_id",
    "event",
    "latency_ms",
}
NUMERIC_FIELDS = {"status", "latency_ms"}
OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    "!=": operator.ne,
    "=": operator.eq,
    ">": operator.gt,
    "<": operator.lt,
}
_COND = re.compile(r"^([a-z_]+)(>=|<=|!=|=|>|<)(.+)$")
_SAFE_VALUE = re.compile(r"^[\w./{}:\-]{1,120}$")


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: str | float

    def matches(self, entry: dict[str, Any]) -> bool:
        actual = entry.get(self.field)
        if actual is None:
            return self.op == "!="
        if self.field in NUMERIC_FIELDS:
            try:
                actual = float(actual)
            except (TypeError, ValueError):
                return False
        return OPS[self.op](actual, self.value)


@dataclass(frozen=True)
class LogFilter:
    conditions: tuple[Condition, ...] = ()
    terms: tuple[str, ...] = field(default=())

    def matches(self, entry: dict[str, Any]) -> bool:
        if not all(c.matches(entry) for c in self.conditions):
            return False
        haystack = f"{entry.get('message', '')} {entry.get('stack', '')}".lower()
        return all(term in haystack for term in self.terms)


def parse_query(query: str) -> LogFilter:
    if len(query) > 200:
        raise QueryError("query too long (max 200 characters)")
    try:
        tokens = shlex.split(query)
    except ValueError as exc:
        raise QueryError(f"could not parse query: {exc}") from exc
    conditions: list[Condition] = []
    terms: list[str] = []
    for token in tokens:
        match = _COND.match(token)
        if not match:
            terms.append(token.lower()[:100])
            continue
        name, op, raw = match.groups()
        if name not in ALLOWED_FIELDS:
            raise QueryError(
                f"unknown field '{name}'. Allowed: {', '.join(sorted(ALLOWED_FIELDS))}"
            )
        if not _SAFE_VALUE.match(raw):
            raise QueryError(f"unsupported characters in value for '{name}'")
        if name in NUMERIC_FIELDS:
            try:
                value: str | float = float(raw)
            except ValueError as exc:
                raise QueryError(f"'{name}' needs a number") from exc
        else:
            if op not in ("=", "!="):
                raise QueryError(f"'{name}' only supports = and !=")
            value = raw
        conditions.append(Condition(name, op, value))
    return LogFilter(tuple(conditions), tuple(terms))
