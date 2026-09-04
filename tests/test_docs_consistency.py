import re
import tomllib
from pathlib import Path

from tests.support import Client

from claude_in_codex import __version__
from claude_in_codex.schemas import FINGERPRINT
from claude_in_codex.server import _capabilities_payload, mcp

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = ROOT / "skills" / "collaborating-with-claude" / "SKILL.md"
PACKAGED_SKILL = (
    ROOT / "plugins" / "claude-in-codex" / "skills" / "collaborating-with-claude" / "SKILL.md"
)
SECURITY = ROOT / "SECURITY.md"
CHANGELOG = ROOT / "CHANGELOG.md"
_CHANGELOG_FINGERPRINT_RE = re.compile(r"claude-in-codex/[0-9.]+/schema-\d+")
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


async def test_serverinfo_version_matches_the_release_lockstep():
    """The version hosts see at initialize is the released application version (#89).

    `__version__` reads installed package metadata, so this ties the MCP surface
    back to the one file CI's release-lockstep check greps (`pyproject.toml`).
    Without it, a stale editable install — or a regression to FastMCP's default —
    would let `serverInfo.version` drift from the version being shipped.
    """
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    async with Client(mcp) as client:
        reported = client.server_info.version

    assert __version__ == declared
    assert reported == declared
    assert _capabilities_payload()["version"] == declared


def _workflow(name: str) -> dict:
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def test_publish_cannot_ship_without_the_live_contract_gate():
    """#170: the gate is only a gate if the release depends on it.

    AGENTS.md and COMPATIBILITY.md named `pytest -m integration` the pre-release
    gate for the upstream CLI envelope contract, but publish.yml never ran it and
    ci.yml ran it only on workflow_dispatch. So a release could be cut with the
    gate never having run, and nothing recorded that.

    This is the structural half: build and publish must not be reachable without
    it. Asserted against the workflow rather than trusted to review, because the
    thing being prevented is precisely a human forgetting a manual step."""
    jobs = _workflow("publish.yml")["jobs"]

    assert "live-integration" in jobs, "publish.yml has no live contract gate"
    assert "live-integration" in jobs["build"]["needs"]
    # And nothing downstream routes around it.
    assert "build" in jobs["publish"]["needs"]


def test_the_live_gate_runs_fail_closed_everywhere_it_runs():
    """A skipped suite exits 0, so the flag is what separates the gate from a
    job that reports green for work it never attempted.

    Both call sites must set it, or the manual dispatch run -- the one a human
    reaches for when they want reassurance before a release -- is the weaker
    instrument of the two."""
    for name in ("publish.yml", "ci.yml"):
        jobs = _workflow(name)["jobs"]
        gate = jobs["live-integration"]
        steps = [s for s in gate["steps"] if "pytest -m integration" in str(s.get("run", ""))]
        assert steps, f"{name}: no integration pytest step found"
        for step in steps:
            assert step.get("env", {}).get("CLAUDE_IN_CODEX_REQUIRE_LIVE") == "1", (
                f"{name}: the integration run does not set CLAUDE_IN_CODEX_REQUIRE_LIVE, "
                "so it would pass by skipping"
            )
