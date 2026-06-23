"""Unit tests for the advisory static model catalog."""

from claude_in_codex import cli_contract
from claude_in_codex.claude_models import read_model_catalog
from claude_in_codex.schemas import FINGERPRINT


def test_every_known_slug_matches_the_pattern():
    # KNOWN_MODELS is a trusted constant: a malformed slug is a bug that must fail
    # loudly here, not silently vanish from the catalog.
    bad = [
        slug
        for slug, _display, _kind in cli_contract.KNOWN_MODELS
        if not cli_contract.MODEL_SLUG_PATTERN.match(slug)
    ]
    assert bad == [], f"malformed bundled slugs: {bad}"


def test_known_models_have_valid_kinds_and_both_kinds_present():
    kinds = {kind for _slug, _display, kind in cli_contract.KNOWN_MODELS}
    assert kinds == {"alias", "full"}


def test_read_model_catalog_is_static_and_complete():
    result = read_model_catalog()
    assert result.ok is True
    assert result.source == "static"
    assert result.unavailable_reason is None
    assert result.fingerprint == FINGERPRINT
    assert len(result.models) == len(cli_contract.KNOWN_MODELS)
    assert {m.slug for m in result.models} == {s for s, _d, _k in cli_contract.KNOWN_MODELS}
    assert any(m.kind == "alias" for m in result.models)
    assert any(m.kind == "full" for m in result.models)
    assert result.advisory  # non-empty advisory text


def test_each_model_carries_its_kind_and_display_name():
    by_slug = {m.slug: m for m in read_model_catalog().models}
    assert by_slug["opus"].kind == "alias"
    assert by_slug["opus"].display_name == "Opus (alias → latest Opus)"
    assert by_slug["claude-opus-4-8"].kind == "full"
