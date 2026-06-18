import re
from pathlib import Path

from cc_plugin_codex.schemas import FINGERPRINT

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = ROOT / "skills" / "collaborating-with-claude" / "SKILL.md"
PACKAGED_SKILL = (
    ROOT / "plugins" / "cc-plugin-codex" / "skills" / "collaborating-with-claude" / "SKILL.md"
)
SECURITY = ROOT / "SECURITY.md"
CHANGELOG = ROOT / "CHANGELOG.md"
_CHANGELOG_FINGERPRINT_RE = re.compile(r"cc-plugin-codex/[0-9.]+/schema-\d+")
BARE_ONLY_UNTRUSTED_WORKSPACES = re.compile(
    r"use\s+`?config_mode=bare`?\s+for\s+untrusted\s+workspaces",
    re.IGNORECASE,
)


def test_packaged_claude_skill_matches_source():
    assert PACKAGED_SKILL.read_text() == SOURCE_SKILL.read_text()


def test_safe_mode_guidance_is_not_bare_only():
    docs = (SOURCE_SKILL.read_text(), SECURITY.read_text())
    for text in docs:
        assert "config_mode=safe" in text
        assert "config_mode=bare" in text
        assert BARE_ONLY_UNTRUSTED_WORKSPACES.search(text) is None


def test_changelog_documents_current_fingerprint():
    """The most recent fingerprint named in CHANGELOG must match schemas.FINGERPRINT.

    Guards the release-hygiene gap where FINGERPRINT is bumped but the changelog's
    `schema-NN` line is left stale (or vice versa). The first (topmost) fingerprint
    mention is the one for the latest release section.
    """
    match = _CHANGELOG_FINGERPRINT_RE.search(CHANGELOG.read_text())
    assert match is not None, "CHANGELOG.md names no schema fingerprint"
    assert match.group(0) == FINGERPRINT, (
        f"CHANGELOG.md's latest fingerprint {match.group(0)!r} does not match "
        f"schemas.FINGERPRINT {FINGERPRINT!r}; update the changelog (or the bump)."
    )
