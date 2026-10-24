"""AWS backends: CloudWatch Logs Insights, CloudWatch metrics, GitHub REST API.

Credentials come from the default AWS chain (the EC2 instance role in production);
nothing here reads access keys. Every call is read-only.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from mcp_servers.backends.base import (
    BackendError,
    Commit,
    GroupBy,
    MetricName,
    MetricPoint,
    SearchResult,
)
from mcp_servers.query import NUMERIC_FIELDS, LogFilter

_SAFE_TERM = re.compile(r"^[\w .:/\-]{1,100}$")
QUERY_TIMEOUT_S = 20.0


def _boto3_client(service: str, region: str) -> Any:
    import boto3  # optional dependency: only needed for the AWS backends

    return boto3.client(service, region_name=region)


def to_insights_filter(log_filter: LogFilter) -> str:
    """Translate the whitelisted filter language into a Logs Insights filter clause.

    Field names are whitelisted and values validated by the parser; text terms are
    re-checked here because they are free text. No raw model text reaches the query.
    """
    parts: list[str] = []
    for c in log_filter.conditions:
        op = "=" if c.op == "=" else c.op
        if c.field in NUMERIC_FIELDS:
            parts.append(f"{c.field} {op} {c.value:g}")
        else:
            parts.append(f'{c.field} {op} "{c.value}"')
    for term in log_filter.terms:
        if not _SAFE_TERM.match(term):
            raise BackendError(f"unsupported characters in search term: {term[:40]!r}")
        parts.append(f'tolower(@message) like "{term}"')
    return " and ".join(parts)


class CloudWatchLogsBackend:
    def __init__(self, log_group: str, region: str, client: Any = None) -> None:
        self.log_group = log_group
        self.client = client or _boto3_client("logs", region)

    def _run(self, query: str, minutes: int, limit: int) -> dict[str, Any]:
        end = datetime.now(UTC)
        start = end - timedelta(minutes=minutes)
        try:
            query_id = self.client.start_query(
                logGroupName=self.log_group,
                startTime=int(start.timestamp()),
                endTime=int(end.timestamp()),
                queryString=query,
                limit=limit,
            )["queryId"]
            deadline = time.monotonic() + QUERY_TIMEOUT_S
            while True:
                resp = self.client.get_query_results(queryId=query_id)
                if resp["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
                    break
                if time.monotonic() > deadline:
                    raise BackendError("CloudWatch Logs Insights query timed out")
                time.sleep(0.5)
        except BackendError:
            raise
        except Exception as exc:  # botocore errors: throttling, access denied, ...
            raise BackendError(f"CloudWatch Logs error: {type(exc).__name__}") from exc
        if resp["status"] != "Complete":
            raise BackendError(f"Logs Insights query {resp['status'].lower()}")
        return resp

    def search(self, log_filter: LogFilter, minutes: int, limit: int) -> SearchResult:
        clause = to_insights_filter(log_filter)
        query = "fields @timestamp, @message"
        if clause:
            query += f" | filter {clause}"
        query += f" | sort @timestamp desc | limit {limit}"
        resp = self._run(query, minutes, limit)
        entries: list[dict[str, Any]] = []
        for row in resp["results"]:
            fields = {f["field"]: f["value"] for f in row}
            try:
                entries.append(json.loads(fields.get("@message", "")))
            except json.JSONDecodeError:
                entries.append(
                    {"timestamp": fields.get("@timestamp"), "message": fields.get("@message", "")}
                )
        matched = int(resp.get("statistics", {}).get("recordsMatched", len(entries)))
        return SearchResult(entries=entries, total_matched=matched)

    def count_errors(self, minutes: int, group_by: GroupBy) -> dict[str, int]:
        query = (
            'filter event = "http_request" and status >= 500'
            f" | stats count(*) as n by {group_by} | sort n desc | limit 50"
        )
        resp = self._run(query, minutes, 50)
        counts: dict[str, int] = {}
        for row in resp["results"]:
            fields = {f["field"]: f["value"] for f in row}
            counts[str(fields.get(group_by) or "(none)")] = int(float(fields.get("n", 0)))
        return counts


# Names must match the metric filters created in infra/terraform/cloudwatch.tf.
METRIC_QUERIES: dict[str, list[dict[str, Any]]] = {
    "latency_p95_ms": [{"Id": "m", "metric": "LatencyMs", "stat": "p95", "visible": True}],
    "memory_mb": [{"Id": "m", "metric": "MemoryMb", "stat": "Maximum", "visible": True}],
    "request_rate": [{"Id": "r", "metric": "RequestCount", "stat": "Sum", "visible": False}],
    "error_rate_5xx": [
        {"Id": "e", "metric": "Errors5xx", "stat": "Sum", "visible": False},
        {"Id": "r", "metric": "RequestCount", "stat": "Sum", "visible": False},
    ],
}
EXPRESSIONS = {
    "request_rate": "r / PERIOD(r)",
    "error_rate_5xx": "100 * FILL(e, 0) / r",
}


class CloudWatchMetricsBackend:
    def __init__(self, namespace: str, region: str, client: Any = None) -> None:
        self.namespace = namespace
        self.client = client or _boto3_client("cloudwatch", region)

    def _queries(self, name: MetricName, period_s: int) -> list[dict[str, Any]]:
        queries: list[dict[str, Any]] = [
            {
                "Id": q["Id"],
                "MetricStat": {
                    "Metric": {"Namespace": self.namespace, "MetricName": q["metric"]},
                    "Period": period_s,
                    "Stat": q["stat"],
                },
                "ReturnData": q["visible"],
            }
            for q in METRIC_QUERIES[name]
        ]
        if name in EXPRESSIONS:
            queries.append({"Id": "out", "Expression": EXPRESSIONS[name], "ReturnData": True})
        return queries

    def get_metric(self, name: MetricName, minutes: int, period_s: int) -> list[MetricPoint]:
        end = datetime.now(UTC)
        try:
            resp = self.client.get_metric_data(
                MetricDataQueries=self._queries(name, period_s),
                StartTime=end - timedelta(minutes=minutes),
                EndTime=end,
                ScanBy="TimestampAscending",
            )
        except Exception as exc:
            raise BackendError(f"CloudWatch metrics error: {type(exc).__name__}") from exc
        result = next(r for r in resp["MetricDataResults"] if r["Id"] in ("out", "m"))
        return [
            MetricPoint(
                timestamp=ts.astimezone(UTC).isoformat(timespec="seconds"), value=round(v, 2)
            )
            for ts, v in zip(result["Timestamps"], result["Values"], strict=True)
        ]


class GitHubBackend:
    """Reads commits from a GitHub repository (owner/name). Token optional for public repos."""

    def __init__(
        self, repo: str, token: str | None = None, client: httpx.Client | None = None
    ) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
            raise ValueError("MCP_GITHUB_REPO must look like owner/name")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.repo = repo
        self.http = client or httpx.Client(
            base_url="https://api.github.com", headers=headers, timeout=10
        )

    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self.http.get(path, **kwargs)
        except httpx.HTTPError as exc:
            raise BackendError(f"GitHub API unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise BackendError(f"GitHub API returned {resp.status_code}")
        return resp

    def recent_commits(self, hours: int, limit: int) -> list[Commit]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        items = self._get(
            f"/repos/{self.repo}/commits", params={"since": since, "per_page": limit}
        ).json()
        commits: list[Commit] = []
        for i, item in enumerate(items):
            files: list[str] = []
            if i < 10:  # file lists cost one request each; only for the newest commits
                detail = self._get(f"/repos/{self.repo}/commits/{item['sha']}").json()
                files = [f["filename"] for f in detail.get("files", [])]
            commits.append(
                Commit(
                    sha=item["sha"],
                    author=item["commit"]["author"]["name"],
                    time=item["commit"]["author"]["date"],
                    message=item["commit"]["message"].splitlines()[0],
                    files=files,
                )
            )
        return commits

    def commit_diff(self, sha: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise ValueError("sha must be 7-40 lowercase hex characters")
        return self._get(
            f"/repos/{self.repo}/commits/{sha}", headers={"Accept": "application/vnd.github.diff"}
        ).text
