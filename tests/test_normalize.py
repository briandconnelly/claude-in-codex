import json

import pytest

from claude_in_codex.normalize import build_prompt, extract_json, normalize_envelope
from claude_in_codex.schemas import FINGERPRINT, Meta


def _meta():
    return Meta(
        cwd="/repo",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=180,
        elapsed_ms=10,
        fingerprint=FINGERPRINT,
    )


def _env(inner, **extra):
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(inner) if isinstance(inner, dict) else inner,
        "session_id": "sess-1",
    }
    base.update(extra)
    return json.dumps(base)


def test_build_prompt_review_mentions_json_and_scope():
    p = build_prompt(
        "claude_review_changes",
        payload={"focus": "security", "scope": "working_tree"},
        context_text="diff --git ...",
    )
    assert "JSON" in p
    assert "security" in p
    assert "diff --git" in p


def test_build_prompt_review_mentions_paths_when_scoped():
    p = build_prompt(
        "claude_review_changes",
        payload={"scope": "working_tree", "paths": ["src"]},
        context_text="diff --git ...",
    )
    assert "Path filter applied" in p
    assert "['src']" in p


def test_build_prompt_review_mentions_branch_range_when_head_set():
    p = build_prompt(
        "claude_review_changes",
        payload={"scope": "branch", "base": "main", "head": "feature"},
        context_text="diff --git ...",
    )
    assert "main...feature" in p


def test_build_prompt_adversarial_mentions_paths_when_diff_attached():
    p = build_prompt(
        "claude_adversarial_review",
        payload={"target": "plan", "paths": ["tests"]},
        context_text="diff --git ...",
    )
    assert "Related changes" in p
    assert "Path filter applied" in p
    assert "['tests']" in p


def test_extract_json_from_fenced_block():
    text = 'prose\n```json\n{"verdict": "pass"}\n```\ntrailing'
    assert extract_json(text) == {"verdict": "pass"}


def test_extract_json_plain_object():
    assert extract_json('{"verdict": "fail"}') == {"verdict": "fail"}


def test_extract_json_ignores_prose_braces_before_object():
    text = 'Use {placeholder} in prose, then {"verdict": "pass", "summary": "ok"}.'
    assert extract_json(text) == {"verdict": "pass", "summary": "ok"}


def test_extract_json_handles_braces_inside_strings():
    text = '```json\n{"summary": "literal { brace }", "verdict": "pass"}\n```'
    assert extract_json(text) == {"summary": "literal { brace }", "verdict": "pass"}


def test_extract_json_uses_first_valid_object():
    text = 'bad {not json} good {"verdict": "concerns"} {"verdict": "pass"}'
    assert extract_json(text) == {"verdict": "concerns"}


def test_extract_json_none_when_absent():
    assert extract_json("no json here") is None


def test_normalize_success_envelope():
    inner = {
        "summary": "looks risky",
        "verdict": "concerns",
        "confidence": "medium",
        "findings": [
            {
                "severity": "high",
                "title": "off-by-one",
                "file": "app.py",
                "line": 2,
                "evidence": "a - b",
                "risk": "wrong result",
                "recommendation": "use +",
            }
        ],
        "questions": [],
        "assumptions": [],
    }
    res = normalize_envelope(
        "claude_review_changes",
        _env(inner, modelUsage={"claude-sonnet-4-6": {}}),
        _meta(),
        detail="full",
    )
    assert res["ok"] is True
    assert res["verdict"] == "concerns"
    assert res["findings"][0]["file"] == "app.py"
    assert res["raw_response"]["session_id"] == "sess-1"
    assert res["raw_response"]["model"] == "claude-sonnet-4-6"
    assert res["raw_response"]["text"]


def test_normalize_summary_omits_raw_text():
    inner = {
        "summary": "ok",
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    res = normalize_envelope("claude_ask", _env(inner), _meta(), detail="summary")
    assert "text" not in res["raw_response"]


def test_normalize_clamps_bad_enums():
    inner = {
        "summary": "x",
        "verdict": "definitely-broken",
        "confidence": "ultra",
        "findings": [
            {
                "severity": "spicy",
                "title": "t",
                "evidence": "e",
                "risk": "r",
                "recommendation": "rec",
            }
        ],
    }
    res = normalize_envelope("claude_ask", _env(inner), _meta(), detail="summary")
    assert res["verdict"] == "unknown"
    assert res["confidence"] == "low"
    assert res["findings"][0]["severity"] == "low"


def test_normalize_drops_incomplete_findings():
    inner = {
        "summary": "x",
        "verdict": "pass",
        "confidence": "high",
        "findings": [{"severity": "high", "title": "only a title"}],
    }
    res = normalize_envelope("claude_ask", _env(inner), _meta(), detail="summary")
    assert res["findings"] == []


def test_normalize_permission_denial_with_empty_result():
    env = _env("", permission_denials=[{"tool": "Bash"}])
    res = normalize_envelope("claude_ask", env, _meta(), detail="summary")
    assert res["ok"] is False
    assert res["error"]["code"] == "claude_permission_error"


def test_normalize_invalid_outer_json():
    res = normalize_envelope("claude_ask", "not json", _meta(), detail="summary")
    assert res["ok"] is False
    assert res["error"]["code"] == "invalid_json"


@pytest.mark.parametrize("stdout", ["[]", '"hello"', "123", "true", "null"])
def test_normalize_valid_non_object_json_returns_structured_error(stdout):
    res = normalize_envelope("claude_ask", stdout, _meta(), detail="summary")
    assert res["ok"] is False
    assert res["error"]["code"] == "invalid_json"
    assert "JSON object" in res["error"]["message"]


def test_normalize_unstructured_inner_falls_back():
    res = normalize_envelope("claude_ask", _env("I think this is fine."), _meta(), detail="full")
    assert res["ok"] is True
    assert res["verdict"] == "unknown"
    assert "fine" in res["summary"]


def test_normalize_denials_recorded_on_success():
    inner = {
        "summary": "ok",
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    res = normalize_envelope(
        "claude_ask", _env(inner, permission_denials=[{"tool": "Bash"}]), _meta(), detail="summary"
    )
    assert res["ok"] is True
    assert res["meta"]["permission_denials"] == [{"tool": "Bash"}]


def test_normalize_is_error_uses_result_text_not_subtype():
    env = _env("", is_error=True, subtype="success", result="Rate limited; try later.")
    res = normalize_envelope("claude_ask", env, _meta(), detail="summary")
    assert res["ok"] is False
    assert res["error"]["code"] == "nonzero_exit"
    assert res["error"]["retryable"] is True
    assert "Rate limited" in res["error"]["message"]
    assert "success" not in res["error"]["message"]


@pytest.mark.parametrize(
    ("result", "expected_code", "retryable"),
    [
        ("Budget stop threshold reached.", "budget_exceeded", True),
        ("Authentication required; run claude /login.", "claude_auth_required", False),
        ("Permission denied for tool Read.", "claude_permission_error", False),
        ("Rate limited; try later.", "nonzero_exit", True),
        ("Invalid API key.", "api_key_invalid", False),
    ],
)
def test_zero_exit_is_error_uses_failure_classifier(result, expected_code, retryable):
    env = _env("", is_error=True, subtype="error", result=result)
    res = normalize_envelope("claude_ask", env, _meta(), detail="summary")
    assert res["ok"] is False
    assert res["error"]["code"] == expected_code
    assert res["error"].get("retryable", False) is retryable


def test_non_success_subtype_without_is_error_uses_result_text():
    env = _env("", is_error=False, subtype="error", result="the model declined to answer")
    res = normalize_envelope("claude_ask", env, _meta(), detail="summary")
    assert res["ok"] is False
    assert res["error"]["code"] == "nonzero_exit"
    assert "the model declined" in res["error"]["message"]
    assert "exited 0" not in res["error"]["message"]


def test_non_success_subtype_without_is_error_detects_contract_drift():
    env = _env("", is_error=False, subtype="error", result="error: unknown option '--effort'")
    res = normalize_envelope("claude_ask", env, _meta(), detail="summary")
    assert res["ok"] is False
    assert res["error"]["code"] == "cli_contract_changed"


def test_normalize_string_questions_not_exploded():
    inner = {
        "summary": "x",
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": "not a list",
        "assumptions": [],
    }
    res = normalize_envelope("claude_ask", _env(inner), _meta(), detail="summary")
    assert res["questions"] == []  # a stray string is ignored, not split into chars


def test_normalize_surfaces_cost_and_usage():
    inner = {
        "summary": "ok",
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    res = normalize_envelope(
        "claude_ask",
        _env(inner, total_cost_usd=0.0123, usage={"input_tokens": 100, "output_tokens": 50}),
        _meta(),
        detail="summary",
    )
    assert res["meta"]["cost_usd"] == 0.0123
    assert res["meta"]["usage"]["input_tokens"] == 100
    assert res["meta"]["usage"]["output_tokens"] == 50


def test_normalize_parses_next_steps_and_line_end():
    inner = {
        "summary": "ok",
        "verdict": "concerns",
        "confidence": "medium",
        "next_steps": ["add a regression test", "revert the change"],
        "findings": [
            {
                "severity": "high",
                "title": "t",
                "evidence": "e",
                "risk": "r",
                "recommendation": "rec",
                "line": 10,
                "line_end": 14,
            }
        ],
    }
    res = normalize_envelope("claude_review_changes", _env(inner), _meta(), detail="summary")
    assert res["next_steps"] == ["add a regression test", "revert the change"]
    assert res["findings"][0]["line_end"] == 14


def test_normalize_reports_cost_on_error_envelope():
    # A failed paid call still cost money — cost/usage must ride on the error meta.
    env = _env(
        "",
        is_error=True,
        subtype="success",
        result="Rate limited; try later.",
        total_cost_usd=0.004,
        usage={"input_tokens": 20, "output_tokens": 0},
    )
    res = normalize_envelope("claude_ask", env, _meta(), detail="summary")
    assert res["ok"] is False
    assert res["meta"]["cost_usd"] == 0.004
    assert res["meta"]["usage"]["input_tokens"] == 20


def test_zero_exit_is_error_drift_is_cli_contract_changed():
    # A drift signature can arrive as a zero-exit envelope with is_error=true, not
    # only as a nonzero process exit; normalize_envelope must label it too.
    env = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "subtype": "error",
            "result": "error: unknown option '--effort'",
        }
    )
    out = normalize_envelope("claude_review_changes", env, _meta(), detail="summary")
    assert out["ok"] is False
    assert out["error"]["code"] == "cli_contract_changed"


def test_zero_exit_is_error_without_drift_stays_nonzero_exit():
    env = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "subtype": "error",
            "result": "the model declined to answer",
        }
    )
    out = normalize_envelope("claude_review_changes", env, _meta(), detail="summary")
    assert out["error"]["code"] == "nonzero_exit"
