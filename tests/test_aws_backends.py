"""AWS backends tested with botocore Stubber and httpx MockTransport: no real AWS calls."""

import json
from datetime import UTC, datetime

import boto3
import httpx
import pytest
from botocore.stub import ANY, Stubber

from mcp_servers.backends.aws import (
    CloudWatchLogsBackend,
    CloudWatchMetricsBackend,
    GitHubBackend,
    to_insights_filter,
)
from mcp_servers.backends.base import BackendError
from mcp_servers.query import parse_query


def logs_client():
    return boto3.client(
        "logs", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="x"
    )


def test_insights_translation() -> None:
    clause = to_insights_filter(parse_query('status>=500 path=/orders "keyerror"'))
    assert clause == 'status >= 500 and path = "/orders" and tolower(@message) like "keyerror"'


def test_insights_rejects_unsafe_free_text() -> None:
    with pytest.raises(BackendError):
        to_insights_filter(parse_query('"x\\" | delete"'))


def test_search_runs_query_and_parses_json_messages() -> None:
    client = logs_client()
    stub = Stubber(client)
    line = {"timestamp": "t", "status": 500, "path": "/orders", "message": "POST /orders 500"}
    stub.add_response(
        "start_query",
        {"queryId": "q1"},
        {
            "logGroupName": "/opspilot/shop",
            "startTime": ANY,
            "endTime": ANY,
            "queryString": (
                "fields @timestamp, @message | filter status >= 500"
                " | sort @timestamp desc | limit 5"
            ),
            "limit": 5,
        },
    )
    stub.add_response("get_query_results", {"status": "Running", "results": []}, {"queryId": "q1"})
    stub.add_response(
        "get_query_results",
        {
            "status": "Complete",
            "results": [
                [
                    {"field": "@timestamp", "value": "t"},
                    {"field": "@message", "value": json.dumps(line)},
                ]
            ],
            "statistics": {"recordsMatched": 42.0, "recordsScanned": 100.0, "bytesScanned": 1.0},
        },
        {"queryId": "q1"},
    )
    with stub:
        result = CloudWatchLogsBackend("/opspilot/shop", "ap-south-1", client).search(
            parse_query("status>=500"), 15, 5
        )
    assert result.total_matched == 42 and result.entries[0]["path"] == "/orders"


def test_count_errors_and_failed_query() -> None:
    client = logs_client()
    stub = Stubber(client)
    stub.add_response("start_query", {"queryId": "q"}, None)
    stub.add_response(
        "get_query_results",
        {
            "status": "Complete",
            "results": [[{"field": "path", "value": "/orders"}, {"field": "n", "value": "7"}]],
        },
        None,
    )
    stub.add_response("start_query", {"queryId": "q2"}, None)
    stub.add_response("get_query_results", {"status": "Failed", "results": []}, None)
    with stub:
        backend = CloudWatchLogsBackend("/opspilot/shop", "ap-south-1", client)
        assert backend.count_errors(15, "path") == {"/orders": 7}
        with pytest.raises(BackendError, match="failed"):
            backend.count_errors(15, "path")


def test_access_denied_becomes_backend_error() -> None:
    client = logs_client()
    stub = Stubber(client)
    stub.add_client_error("start_query", "AccessDeniedException")
    with stub, pytest.raises(BackendError, match="AccessDenied"):
        CloudWatchLogsBackend("/opspilot/shop", "ap-south-1", client).count_errors(15, "path")


def test_error_rate_uses_metric_math() -> None:
    client = boto3.client(
        "cloudwatch", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    stub = Stubber(client)
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    stub.add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                {
                    "Id": "out",
                    "Label": "out",
                    "Timestamps": [ts],
                    "Values": [12.5],
                    "StatusCode": "Complete",
                }
            ]
        },
        None,
    )
    with stub:
        backend = CloudWatchMetricsBackend("OpsPilot/Shop", "ap-south-1", client)
        points = backend.get_metric("error_rate_5xx", 30, 60)
    assert points[0].value == 12.5
    queries = backend._queries("error_rate_5xx", 60)
    assert queries[-1]["Expression"] == "100 * FILL(e, 0) / r"


def test_github_backend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/r/commits":
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "a" * 40,
                        "commit": {
                            "author": {"name": "Dev", "date": "2026-10-24T10:00:00Z"},
                            "message": "release v1.5.0\n\nbody",
                        },
                    }
                ],
            )
        if request.headers.get("accept") == "application/vnd.github.diff":
            return httpx.Response(200, text="diff --git a/x b/x")
        return httpx.Response(200, json={"files": [{"filename": "shop/pricing.py"}]})

    gh = GitHubBackend(
        "o/r",
        client=httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ),
    )
    commits = gh.recent_commits(24, 5)
    assert commits[0].message == "release v1.5.0" and commits[0].files == ["shop/pricing.py"]
    assert gh.commit_diff("a" * 40).startswith("diff --git")
    with pytest.raises(ValueError):
        GitHubBackend("not a repo")
