from claude_in_codex.schemas import (
    FINGERPRINT,
    RESULT_SCHEMA,
    ErrorInfo,
    ErrorResult,
    Finding,
    Meta,
    RawResponse,
    SuccessResult,
)


def test_usage_model_is_closed_and_optional():
    from claude_in_codex.schemas import Usage

    u = Usage()  # all fields optional
    assert u.input_tokens is None
    assert Usage.model_json_schema().get("additionalProperties") is False


def test_meta_carries_cost_and_usage_fields():
    from claude_in_codex.schemas import Meta, Usage

    m = Meta(
        cwd="/x",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=10,
        elapsed_ms=1,
        cost_usd=0.5,
        usage=Usage(input_tokens=3, output_tokens=4),
    )
    dumped = m.model_dump(mode="json", exclude_none=True)
    assert dumped["cost_usd"] == 0.5
    assert dumped["usage"]["input_tokens"] == 3


def test_meta_carries_redacted_paths():
    m = Meta(
        cwd="/x",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=10,
        elapsed_ms=1,
        redacted_paths=["app.py"],
    )
    assert m.model_dump(mode="json", exclude_none=True)["redacted_paths"] == ["app.py"]


def test_meta_carries_security_warnings():
    m = Meta(
        cwd="/x",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=10,
        elapsed_ms=1,
        security_warnings=["workspace hooks present"],
    )
    assert m.model_dump(mode="json", exclude_none=True)["security_warnings"] == [
        "workspace hooks present"
    ]


def test_finding_supports_line_range():
    from claude_in_codex.schemas import Finding

    f = Finding(
        severity="low",
        title="t",
        evidence="e",
        risk="r",
        recommendation="rec",
        line=10,
        line_end=14,
    )
    assert f.line == 10 and f.line_end == 14


def test_success_result_has_next_steps():
    from claude_in_codex.schemas import Meta, SuccessResult

    r = SuccessResult(
        tool="claude_ask",
        summary="s",
        verdict="pass",
        confidence="high",
        next_steps=["do x"],
        meta=Meta(
            cwd="/x", config_mode="inherit", access="toolless", timeout_seconds=10, elapsed_ms=1
        ),
    )
    assert r.next_steps == ["do x"]


def test_fingerprint_value():
    assert FINGERPRINT == "claude-in-codex/0.1/schema-22"


def test_meta_carries_head_and_diff_range():
    meta = Meta(
        cwd="/repo",
        config_mode="inherit",
        access="toolless",
        scope="branch",
        base="main",
        head="feature",
        diff_range="main...feature",
        timeout_seconds=180,
        elapsed_ms=10,
    )
    assert meta.head == "feature"
    assert meta.diff_range == "main...feature"


def test_success_result_dump_omits_none():
    meta = Meta(
        cwd="/repo",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=180,
        elapsed_ms=10,
        fingerprint=FINGERPRINT,
    )
    res = SuccessResult(
        tool="claude_ask",
        summary="s",
        verdict="pass",
        confidence="high",
        findings=[Finding(severity="low", title="t", evidence="e", risk="r", recommendation="rec")],
        raw_response=RawResponse(),
        meta=meta,
    )
    dumped = res.model_dump(mode="json", exclude_none=True)
    assert dumped["ok"] is True
    assert "text" not in dumped["raw_response"]  # None text dropped
    assert "file" not in dumped["findings"][0]  # None file dropped


def test_error_result_shape():
    err = ErrorResult(
        error=ErrorInfo(code="timeout", message="m", repair="r"),
        meta=Meta(
            cwd="/repo",
            config_mode="inherit",
            access="toolless",
            timeout_seconds=180,
            elapsed_ms=1,
            fingerprint=FINGERPRINT,
        ),
    )
    dumped = err.model_dump(mode="json", exclude_none=True)
    assert dumped["ok"] is False
    assert dumped["error"]["code"] == "timeout"


def test_meta_carries_request_id():
    # F7: every Meta gets a correlation id so failures can be tied to their call.
    meta = Meta(
        cwd="/repo",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=180,
        elapsed_ms=1,
        fingerprint=FINGERPRINT,
    )
    dumped = meta.model_dump(mode="json", exclude_none=True)
    assert dumped.get("request_id")
    other = Meta(
        cwd="/repo",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=180,
        elapsed_ms=1,
        fingerprint=FINGERPRINT,
    )
    assert other.request_id != meta.request_id  # unique per construction


def test_error_info_drops_misleading_retry_after_ms():
    # F7: retry_after_ms implied a backoff delay we never compute for budget/timeout.
    assert "retry_after_ms" not in ErrorInfo.model_fields


def test_success_result_schema_is_closed():
    assert SuccessResult.model_json_schema().get("additionalProperties") is False


def test_error_result_schema_is_closed():
    assert ErrorResult.model_json_schema().get("additionalProperties") is False


def test_result_schema_defs_are_closed():
    import json

    blob = json.dumps(RESULT_SCHEMA)
    # Nested object models (Finding, Meta, ErrorInfo, ...) carry the closed flag.
    assert '"additionalProperties": false' in blob
