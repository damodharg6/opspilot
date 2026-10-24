"""Mask secrets and personal data before text reaches an LLM or long-term storage.

Pattern-based redaction is a safety net, not a guarantee: the first line of defence
is not logging secrets at all.
"""

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # bearer tokens (before key=value, so "Authorization: Bearer x" loses the token)
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    # key=value / key: value pairs whose key looks secret
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|access[_-]?key)"
            r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\"',;&]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    # credentials inside connection URLs: scheme://user:pass@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)"), r"\1[REDACTED]\3"),
    # AWS access key ids
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # GitHub / OpenAI / Groq style tokens
    (re.compile(r"\b(ghp|gho|ghs|github_pat|sk|gsk)_[A-Za-z0-9_-]{16,}\b"), "[REDACTED_TOKEN]"),
    # e-mail addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
