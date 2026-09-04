"""Shared test fixtures and helpers."""

import json
import os
import subprocess

import fastmcp
import pytest

# FastMCP 4 bridges the MCP SDK v1 camelCase field names (`inputSchema`,
# `readOnlyHint`, ...) onto the SDK v2 snake_case fields with a deprecation
# warning, and the bridge is scheduled for removal. Turn it off for the whole
# suite so any remaining camelCase read fails as a hard AttributeError here
# instead of surviving on a shim that a later upgrade drops.
fastmcp.settings.mcp_camelcase_compat = False


@pytest.fixture(autouse=True, scope="session")
def _no_inherited_git_env():
    """Drop inherited GIT_* variables for the whole test session.

    Tests build throwaway repositories by running git in `tmp_path`. Git's own
    environment variables override repository discovery, so if the pytest process
    inherits GIT_DIR (a git hook exports it — this is why the `pre-push` hook ran
    the suite against the real repository), those commands target the real repo
    instead: fixture files get staged into its index and every tracked file shows
    as deleted.

    Scrubbing the process environment, rather than passing `env=` at each of the
    ~40 git call sites, fixes the ad-hoc ones too and cannot be forgotten by the
    next test that shells out to git."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("GIT_")}
    for key in saved:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        # Drop any GIT_* introduced during the run before restoring, so teardown
        # and late session hooks see exactly the environment we started with.
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            os.environ.pop(key, None)
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _emitted_details_are_advertised(monkeypatch):
    """Every `details` field an error actually emits must be in its catalog entry.

    `ErrorCodeDoc.detail_fields` is published as "the fields this code may
    populate", so an agent that pre-builds a branch from the catalog never learns
    about a field the catalog omits. `test_catalog_detail_fields_exist_on_error_details`
    only checks the other direction -- that advertised names are real -- which is
    why `invalid_workspace_root` could emit the `reason` carrying the whole
    sessionless-connection contract while advertising only `field`/`value`.

    Checked here, at the `_err` boundary, rather than by a static walk or by one
    specimen per code. A static walk cannot see the codes `_workspace_error`
    passes through a variable, the `X and ErrorDetails(...)` sites, or the shared
    `_oversized_diff_details` helper; and one specimen per code proves nothing
    about a code whose branches emit different shapes -- `invalid_workspace_root`
    alone emits `field`/`value` on one branch and `field`/`reason` on another.
    Wrapping the builder covers every branch the suite reaches, and it fails in
    the test that reached it rather than in a summary at session end.

    This is an assertion about the SERVER's own catalog, so it is keyed off the
    envelope `_err` returns (post-merge, `exclude_none`) -- exactly the fields
    that ship."""
    import claude_in_codex.server as srv

    advertised = {row[0]: set(row[3]) for row in srv._ERROR_CATALOG}
    real_err = srv._err

    def checked_err(code, *args, **kwargs):
        envelope = real_err(code, *args, **kwargs)
        emitted = set((envelope.get("error") or {}).get("details") or {})
        unadvertised = emitted - advertised.get(code, set())
        assert not unadvertised, (
            f"{code} emits details {sorted(unadvertised)} that its _ERROR_CATALOG "
            f"entry does not advertise (advertised: {sorted(advertised.get(code, set()))}). "
            "Add them to the catalog entry and bump FINGERPRINT."
        )
        return envelope

    monkeypatch.setattr(srv, "_err", checked_err)


def structured(result):
    """Extract the structured payload from a FastMCP call result across versions."""
    data = getattr(result, "structured_content", None)
    if data is not None:
        return data
    return json.loads(result.content[0].text)


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway git repo with one committed file and one unstaged change."""

    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "Test")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    run("add", "app.py")
    run("commit", "-q", "-m", "init")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    return tmp_path


@pytest.fixture
def fake_claude(monkeypatch):
    """Replace server.run_claude_async so tests never invoke the real CLI or incur cost."""
    import claude_in_codex.server as srv
    from claude_in_codex.claude import ClaudeRun

    inner = {
        "summary": "off-by-one bug",
        "verdict": "concerns",
        "confidence": "high",
        "findings": [
            {
                "severity": "high",
                "title": "subtraction",
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
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(inner),
            "session_id": "sess-1",
            "modelUsage": {"claude-sonnet-4-6": {}},
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )

    async def fake_run(cmd, cwd, timeout_seconds, stdin_text=None, *, config_mode=None):
        return ClaudeRun(stdout=envelope, stderr="", exit_code=0, elapsed_ms=12, timed_out=False)

    monkeypatch.setattr(srv, "run_claude_async", fake_run)
    return envelope


# --- Live release gate (#170) -------------------------------------------------
# AGENTS.md and COMPATIBILITY.md make `pytest -m integration` the gate for the
# half of the `claude` CLI contract no-spend tests cannot cover. #159 made its
# assertions real. Nothing ran it: dispatch-only in CI, absent from publish.yml,
# and DESELECTED locally by addopts -- so a green `uv run pytest` printed
# "4 deselected" and there was no signal distinguishing "ran and passed" from
# "never ran".
#
# Lifting the skips is not enough. Every test in that suite could still be
# skipped, or zero could be collected after a rename, and pytest would exit 0.
# An exit code that cannot tell "verified" from "never attempted" is the exact
# failure the gate exists to prevent, one level up.
#
# So under CLAUDE_IN_CODEX_REQUIRE_LIVE the session must ALSO prove a floor of
# integration tests actually passed.
# Counts CONTRACT tests only. test_live_gate_prerequisites_are_present carries the
# module-level integration marker but exercises no Claude envelope, so counting it
# would let one real contract test be renamed, removed or skipped while the floor
# still reported four passes -- the prerequisite plus three. That is precisely the
# regression the floor exists to catch, so the floor would have been self-defeating.
# Raised by review of the first draft.
_LIVE_GATE_MIN_PASSED = 4
_LIVE_GATE_EXCLUDED = {"test_live_gate_prerequisites_are_present"}


def pytest_runtest_logreport(report):
    if report.when != "call" or not report.passed or "integration" not in report.keywords:
        return
    if any(name in report.nodeid for name in _LIVE_GATE_EXCLUDED):
        return
    _LIVE_GATE_PASSED.append(report.nodeid)


_LIVE_GATE_PASSED: list[str] = []


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("CLAUDE_IN_CODEX_REQUIRE_LIVE") != "1":
        return
    # The counter is process-local. Under pytest-xdist each worker would count its
    # own share and every one of them would fall short, so the gate would fail
    # closed rather than pass wrongly -- the safe direction, but a confusing
    # failure. Refuse the combination explicitly instead of leaving it to be
    # discovered during a release.
    if getattr(session.config, "workerinput", None) is not None or session.config.getoption(
        "numprocesses", default=None
    ):
        session.exitstatus = 1
        print(
            "\nLIVE GATE FAILED: the release gate does not support pytest-xdist; "
            "its pass counter is process-local. Run it without -n."
        )
        return
    passed = len(_LIVE_GATE_PASSED)
    if passed < _LIVE_GATE_MIN_PASSED:
        session.exitstatus = 1
        print(
            f"\nLIVE GATE FAILED: {passed} integration test(s) passed, "
            f"expected at least {_LIVE_GATE_MIN_PASSED}.\n"
            "A release gate that passes by skipping or collecting nothing reports "
            "'verified' for work that was never attempted. If the suite legitimately "
            "shrank, lower _LIVE_GATE_MIN_PASSED in tests/conftest.py deliberately."
        )
