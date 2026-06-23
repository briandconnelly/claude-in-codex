"""Build the advisory static model catalog for `model`-slug discovery.

Unlike the sibling codex-in-claude project (which reads an on-disk Codex cache),
Claude Code writes no model cache, so this is bundled-static only: it turns the
trusted KNOWN_MODELS constant into a ModelCatalogResult with no filesystem access.
Discovery only — the result is explicitly advisory; `claude` validates the real slug.
"""

from __future__ import annotations

from typing import cast

from claude_in_codex import cli_contract
from claude_in_codex.schemas import ModelCatalogResult, ModelInfo, ModelKind

_ADVISORY = (
    "Advisory model list for the `model` param — not authoritative. The `claude` CLI "
    "validates the slug at run time; an unlisted slug may still work and a listed one "
    'may be unavailable to your account. Prefer alias slugs (kind="alias", e.g. '
    "'opus'/'sonnet'), which track the latest model, over pinned full IDs that go stale."
)
_UNAVAILABLE = (
    "No model catalog is bundled. Pass a known Claude model slug directly — an alias "
    "like 'opus'/'sonnet'/'haiku'/'fable' or a full model ID; it is validated at run time."
)


def read_model_catalog() -> ModelCatalogResult:
    """The advisory static model catalog (no live cache exists for Claude).

    KNOWN_MODELS is trusted, so entries are NOT silently filtered by the slug pattern
    here — a malformed constant is caught loudly by tests/test_claude_models.py instead.
    """
    models = [
        ModelInfo(slug=slug, display_name=display, kind=cast("ModelKind", kind))
        for slug, display, kind in cli_contract.KNOWN_MODELS
    ]
    if models:
        return ModelCatalogResult(source="static", models=models, advisory=_ADVISORY)
    return ModelCatalogResult(source="none", advisory=_ADVISORY, unavailable_reason=_UNAVAILABLE)
