"""Golden-file test pinning the `claude -p --output-format json` envelope shape.

If an upstream rename of an envelope key (or a refactor of normalize.py) breaks
parsing, this fails loudly against a recorded real envelope — without needing the
live CLI. Update tests/golden/claude_envelope.json when the upstream shape
legitimately changes."""

from pathlib import Path

from claude_in_codex.normalize import normalize_envelope
from claude_in_codex.schemas import (
    _META_STUB,
    FINGERPRINT,
    Meta,
    meta_fields_from_description,
)

_GOLDEN = (Path(__file__).parent / "golden" / "claude_envelope.json").read_text()


def _meta():
    return Meta(
        cwd="/repo",
        config_mode="inherit",
        access="toolless",
        timeout_seconds=180,
        elapsed_ms=10,
        configured_max_budget_usd=99.0,
        effective_max_budget_usd=5.0,
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
    assert out["meta"]["configured_max_budget_usd"] == 99.0
    assert out["meta"]["effective_max_budget_usd"] == 5.0


def test_golden_envelope_summary_is_bounded_and_a_subset_of_full():
    """The recorded real envelope must render the same at both densities (#94).

    Small enough to fit inside every cap, so this pins the no-truncation path
    against a real payload — if it started truncating, the bounds would be wrong."""
    summary = normalize_envelope("claude_review_changes", _GOLDEN, _meta(), detail="summary")
    full = normalize_envelope("claude_review_changes", _GOLDEN, _meta(), detail="full")
    assert "truncation" not in summary
    assert "truncation" not in full
    assert set(summary) <= set(full)
    assert summary["findings"] == full["findings"]
    assert "text" not in summary["raw_response"]
    assert full["raw_response"]["text"]


def test_golden_envelope_meta_keys_are_declared_and_disclosed():
    """Every `meta` key on a real envelope must be a declared, advertised field.

    Required by AGENTS.md: an agent-visible surface change updates the
    golden-envelope tests in the same PR. This is the coverage that earns its
    place here rather than a re-pin -- it ties the envelope an agent actually
    receives to both the model and the advertised enumeration agents read to
    know what `meta` holds.

    It closes the same class of drift as #143 one layer down. The enumeration is
    generated from `Meta.model_fields`, so model-vs-description cannot diverge;
    this adds envelope-vs-both, which would catch a key that reached `meta`
    without being declared -- exactly what the fingerprint digest cannot see,
    because `meta` is advertised as an opaque stub.

    Subset, not equality: `meta` is dumped with `exclude_none`, so a real
    envelope carries only the fields that were set."""
    out = normalize_envelope("claude_review_changes", _GOLDEN, _meta(), detail="full")
    described = meta_fields_from_description(_META_STUB["description"])

    keys = set(out["meta"])
    assert keys, "an empty meta would make both assertions below vacuous"
    assert keys <= set(Meta.model_fields), keys - set(Meta.model_fields)
    assert keys <= set(described), keys - set(described)
