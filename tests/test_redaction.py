from common.redaction import redact


def test_redacts_common_secrets() -> None:
    text = (
        "password=hunter2 token: abc123 "
        "url=postgresql://shop:s3cret@db:5432/shop "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y "
        "key AKIAABCDEFGHIJKLMNOP and ghp_aaaaaaaaaaaaaaaaaaaaaaaa "
        "contact ravi@example.com"
    )
    out = redact(text)
    for secret in ("hunter2", "abc123", "s3cret", "eyJhbGci", "AKIAABCD", "ghp_aaaa", "ravi@"):
        assert secret not in out
    assert "postgresql://shop:[REDACTED]@db" in out


def test_leaves_normal_text_alone() -> None:
    text = "POST /orders 500 KeyError latency_ms=12.5"
    assert redact(text) == text
