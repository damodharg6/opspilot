"""CloudWatch alarm -> SNS -> this Lambda -> POST /incidents.

Standard library only (no packaging step). Runs inside the VPC and calls the
OpsPilot API on its private IP, so the API port is never exposed to the internet.

Environment:
    OPSPILOT_URL         e.g. http://10.0.1.23:8080
    OPSPILOT_API_SECRET  shared secret for X-OpsPilot-Secret
    SERVICE_NAME         service the alarms belong to (default: shop)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger()
log.setLevel(logging.INFO)

TIMEOUT_S = 8


def parse_alarm(sns_message: str) -> dict[str, Any] | None:
    """Return an incident payload, or None for non-ALARM transitions (OK, INSUFFICIENT_DATA)."""
    alarm = json.loads(sns_message)
    if alarm.get("NewStateValue") != "ALARM":
        return None
    return {
        "service": os.environ.get("SERVICE_NAME", "shop"),
        "alarm_name": alarm["AlarmName"][:200],
        "source": "cloudwatch",
        "description": (alarm.get("NewStateReason") or "")[:2000],
        "alarm_time": alarm.get("StateChangeTime"),
    }


def post_incident(payload: dict[str, Any]) -> int:
    request = urllib.request.Request(  # noqa: S310 - URL comes from our own configuration
        url=os.environ["OPSPILOT_URL"].rstrip("/") + "/incidents",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-OpsPilot-Secret": os.environ["OPSPILOT_API_SECRET"],
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:  # noqa: S310
        return resp.status


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    forwarded, skipped = 0, 0
    for record in event.get("Records", []):
        payload = parse_alarm(record["Sns"]["Message"])
        if payload is None:
            skipped += 1
            continue
        try:
            status = post_incident(payload)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise  # let Lambda's async retry deliver it again
            log.error("OpsPilot rejected alarm %s: HTTP %s", payload["alarm_name"], exc.code)
            continue
        log.info("forwarded alarm %s: HTTP %s", payload["alarm_name"], status)
        forwarded += 1
    return {"forwarded": forwarded, "skipped": skipped}
