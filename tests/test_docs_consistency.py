from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = ROOT / "skills" / "collaborating-with-claude" / "SKILL.md"
PACKAGED_SKILL = (
    ROOT / "plugins" / "cc-plugin-codex" / "skills" / "collaborating-with-claude" / "SKILL.md"
)
SECURITY = ROOT / "SECURITY.md"


def test_packaged_claude_skill_matches_source():
    assert PACKAGED_SKILL.read_text() == SOURCE_SKILL.read_text()


def test_safe_mode_guidance_is_not_bare_only():
    docs = (SOURCE_SKILL.read_text(), SECURITY.read_text())
    for text in docs:
        assert "config_mode=safe` or `config_mode=bare" in text
        assert "use `config_mode=bare` for untrusted workspaces" not in text
