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
            env = step.get("env", {})
            assert env.get("CLAUDE_IN_CODEX_REQUIRE_LIVE") == "1", (
                f"{name}: the integration run does not set CLAUDE_IN_CODEX_REQUIRE_LIVE, "
                "so it would pass by skipping"
            )
            # And it must be handed the credential the contract tests can
            # actually use. The tests run under config_mode=inherit/safe, which
            # STRIP ANTHROPIC_API_KEY, so a job passing only that key fails at
            # the prerequisite -- correctly, but on every run, which turns a gate
            # into a permanent release block. The first revision of this job did
            # exactly that: its comments described a login-backed credential it
            # never passed.
            assert "CLAUDE_CODE_OAUTH_TOKEN" in env, (
                f"{name}: the live gate passes no login-backed credential. "
                "config_mode=inherit and safe strip ANTHROPIC_API_KEY, so this job "
                "would fail its prerequisite on every run and block every release."
            )


def test_every_integration_test_is_deliberately_counted_or_excluded():
    """The live gate's floor is only meaningful if its membership is a RULE.

    #170 shipped a floor that counted a credential check; review found it, and I
    excluded that one name. `test_status_live` had the identical defect — CLI
    readiness, not the upstream envelope — and a later review found that too. The
    first fix addressed an instance where the problem was a class, and a
    hand-maintained blocklist reproduces that indefinitely: the next non-envelope
    test added to the file silently inflates the floor and lets a real envelope
    test be removed without failing.

    This makes membership total. Every integration test must be either counted or
    explicitly excluded, so adding one forces a deliberate decision instead of a
    default. It also pins the floor to the number actually counted, so the two
    cannot drift."""
    from tests.conftest import _LIVE_GATE_EXCLUDED, _LIVE_GATE_MIN_PASSED

    source = (ROOT / "tests" / "test_integration.py").read_text()
    names = set(re.findall(r"^(?:async )?def (test_\w+)", source, re.M))
    assert names, "no integration tests found — the assertions below would be vacuous"

    unknown = _LIVE_GATE_EXCLUDED - names
    assert not unknown, f"excluded names that no longer exist: {unknown}"

    counted = names - _LIVE_GATE_EXCLUDED
    assert len(counted) == _LIVE_GATE_MIN_PASSED, (
        f"the floor is {_LIVE_GATE_MIN_PASSED} but {len(counted)} tests would count "
        f"({sorted(counted)}). A new integration test must either exercise the "
        "upstream envelope — and raise the floor — or be added to "
        "_LIVE_GATE_EXCLUDED. Silently counting a non-envelope test lets a real "
        "one be removed while the floor still clears."
    )

    # Counted means it pins the envelope. The two excluded tests are excluded
    # precisely because they do not call this helper.
    for name in sorted(counted):
        body = source[source.index(f"def {name}") :]
        body = body[: body.find("\nasync def ") if "\nasync def " in body[1:] else len(body)]
        assert "_assert_structured" in body, (
            f"{name} counts toward the envelope floor but never calls "
            "_assert_structured, so it does not pin the upstream result contract"
        )


def test_every_job_that_installs_npm_packages_drops_the_checkout_token():
    """A freshly fetched npm package runs lifecycle scripts in the workspace.

    `actions/checkout` leaves the job's GitHub token in git config by default, so
    those scripts can read it. The `claude-contract` job has guarded against this
    since it was written; the two live-integration jobs added by #170 did the
    identical `npm install` without the guard, and review caught it.

    Asserted for every job rather than for the two that were named. The defect is
    that a job installing third-party code inherits a credential it never needs —
    which is a property of the job, not of these three, and the next one to do it
    should fail here rather than in review."""
    for name in ("ci.yml", "publish.yml"):
        jobs = _workflow(name)["jobs"]
        for job_name, job in jobs.items():
            steps = job.get("steps") or []
            # Every way a job can fetch and execute third-party JS, not just the
            # one spelling this repo happens to use today. The commit that added
            # this guard claimed it covered "EVERY job that runs npm install";
            # matching one literal made that claim false for `npm ci`, `npx`,
            # `pnpm` and `yarn`.
            fetchers = ("npm install", "npm i ", "npm ci", "npx ", "pnpm ", "yarn ")
            installs = [s for s in steps if any(f in str(s.get("run", "")) for f in fetchers)]
            if not installs:
                continue
            checkouts = [s for s in steps if "actions/checkout" in str(s.get("uses", ""))]
            assert checkouts, f"{name}:{job_name} installs npm packages without a checkout?"
            for step in checkouts:
                assert (step.get("with") or {}).get("persist-credentials") is False, (
                    f"{name}:{job_name} fetches and runs third-party JS but its "
                    "checkout keeps the "
                    "GitHub token in git config, where a package's lifecycle scripts "
                    "can read it. Set persist-credentials: false."
                )
