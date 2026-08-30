import hashlib

from claude_in_codex import schemas
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
    assert FINGERPRINT == "claude-in-codex/0.1/schema-37"


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


def test_retry_after_ms_defaults_to_none():
    """The field exists again (#60), but only where a real delay is computed.

    It was previously dropped because a defaulted backoff would be a number the
    server invented. The invariant that made that right is kept as a default of
    None — "no delay is known" — so only a call site with an actual figure (today:
    job_running, which reuses the job poll interval) ever sets it."""
    assert ErrorInfo(code="timeout", message="m", repair="r").retry_after_ms is None


def test_success_result_schema_is_closed():
    assert SuccessResult.model_json_schema().get("additionalProperties") is False


def test_error_result_schema_is_closed():
    assert ErrorResult.model_json_schema().get("additionalProperties") is False


def test_result_schema_defs_are_closed():
    import json

    blob = json.dumps(RESULT_SCHEMA)
    # Nested object models (Finding, Meta, ErrorInfo, ...) carry the closed flag.
    assert '"additionalProperties": false' in blob


def test_default_next_step_covers_every_error_code():
    """A code missing from the table would silently fall back to no_automatic_repair,
    telling agents nothing can be done about an error that is in fact repairable."""
    from typing import get_args

    from claude_in_codex.schemas import DEFAULT_NEXT_STEP, ErrorCode

    assert sorted(DEFAULT_NEXT_STEP) == sorted(get_args(ErrorCode))


def test_explicit_retryable_overrides_the_default_step():
    from claude_in_codex.schemas import ErrorInfo

    # invalid_base defaults to retry_with_changes...
    assert ErrorInfo(code="invalid_base", message="m", repair="r").action.next_step == (
        "retry_with_changes"
    )
    # ...but a site asserting the same call can succeed later gets retry_same_call.
    assert (
        ErrorInfo(code="invalid_base", message="m", repair="r", retryable=True).action.next_step
        == "retry_same_call"
    )


def test_explicit_action_is_never_overwritten():
    from claude_in_codex.schemas import ErrorInfo, RepairAction

    info = ErrorInfo(
        code="job_not_found",
        message="m",
        repair="r",
        action=RepairAction(next_step="call_tool", tool="claude_job_list"),
    )
    assert info.action.tool == "claude_job_list"


def test_system_prompt_append_meta_records_hash_and_length():
    fp = schemas.SystemPromptAppend.of("Only auth findings.")
    assert fp.bytes == len(b"Only auth findings.")
    assert fp.sha256 == hashlib.sha256(b"Only auth findings.").hexdigest()


def test_meta_omits_system_prompt_append_by_default():
    meta = schemas.Meta(
        cwd="/w", config_mode="inherit", access="toolless", timeout_seconds=1, elapsed_ms=0
    )
    assert meta.system_prompt_append is None


def test_meta_carries_system_prompt_append_fingerprint():
    meta = schemas.Meta(
        cwd="/w",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=1,
        elapsed_ms=0,
        system_prompt_append=schemas.SystemPromptAppend.of("persona"),
    )
    assert meta.system_prompt_append is not None
    assert meta.system_prompt_append.bytes == 7
