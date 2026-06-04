"""Golden-file test pinning the `claude -p --output-format json` envelope shape.

If an upstream rename of an envelope key (or a refactor of normalize.py) breaks
parsing, this fails loudly against a recorded real envelope — without needing the
live CLI. Update tests/golden/claude_envelope.json when the upstream shape
legitimately changes."""

from pathlib import Path

from cc_plugin_codex.normalize import normalize_envelope
from cc_plugin_codex.schemas import FINGERPRINT, Meta

_GOLDEN = (Path(__file__).parent / "golden" / "claude_envelope.json").read_text()


def _meta():
    return Meta(
        cwd="/repo",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=180,
        elapsed_ms=10,
        fingerprint=FINGERPRINT,
    )


def test_golden_envelope_parses_to_success_with_cost():
    out = normalize_envelope("claude_review_changes", _GOLDEN, _meta(), detail="full")
    assert out["ok"] is True
    assert out["verdict"] == "concerns"
    assert out["confidence"] == "high"
    assert out["findings"][0]["severity"] == "high"
    # Cost and usage must be plumbed off the envelope onto meta.
    assert out["meta"]["cost_usd"] == 0.0123
    assert out["meta"]["usage"]["input_tokens"] == 100
    assert out["meta"]["usage"]["cache_read_input_tokens"] == 10
