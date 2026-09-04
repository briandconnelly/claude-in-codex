"""Integration tests — require the real `claude` CLI (gated via skipif)."""

import os
import shutil
import subprocess

import pytest
from tests.conftest import structured
from tests.support import Client

from claude_in_codex.server import mcp

# The release gate's fail-closed switch (#170). Off, a missing `claude` SKIPS, which
# is right for a developer machine. On, a missing prerequisite must FAIL: this suite
# is the only check covering the upstream CLI envelope contract (ENVELOPE_KEYS,
# SUCCESS_SUBTYPES, USAGE_KEYS), and a gate that reports green because it never ran
# is worse than no gate -- it launders "unverified" into "verified".
REQUIRE_LIVE = os.environ.get("CLAUDE_IN_CODEX_REQUIRE_LIVE") == "1"
_CLAUDE_MISSING = shutil.which("claude") is None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_CLAUDE_MISSING and not REQUIRE_LIVE, reason="claude CLI not installed"),
]


def test_live_gate_prerequisites_are_present():
    """Fails, never skips, when the gate is required.

    Every other test here carries a skipif on the `claude` binary. Under
    CLAUDE_IN_CODEX_REQUIRE_LIVE that guard is lifted, but a lifted skip only
    turns into a confusing downstream error -- this states the missing
    prerequisite plainly instead, and is the first thing to fail."""
    if not REQUIRE_LIVE:
        pytest.skip("release gate not requested (set CLAUDE_IN_CODEX_REQUIRE_LIVE=1)")
    assert not _CLAUDE_MISSING, (
        "CLAUDE_IN_CODEX_REQUIRE_LIVE=1 but the `claude` CLI is not on PATH. The "
        "release gate must not pass by skipping."
    )
    # Probed under the mode the CONTRACT TESTS ACTUALLY USE, not under None.
    #
    # Two earlier revisions got this wrong in opposite directions. The first
    # asserted ANTHROPIC_API_KEY and failed on a developer machine where the live
    # tests had just passed under subscription auth. The second probed
    # config_mode=None -- which is not a login-backed mode, so it inherits the
    # environment and ACCEPTS a bare ANTHROPIC_API_KEY. But every paid test below
    # runs under `inherit` or `safe`, and `_claude_subprocess_env` strips
    # ANTHROPIC_API_KEY in exactly those modes. So a runner holding only that key
    # would pass this prerequisite and then fail all three envelope tests on
    # authentication -- a credential problem wearing the costume of a contract
    # regression, which is the misleading signal this whole gate exists to remove.
    #
    # Probing `inherit` means a key-only runner fails HERE, first, saying so.
    from claude_in_codex.claude import auth_status

    logged_in, detail = auth_status(config_mode="inherit")
    assert logged_in, (
        "CLAUDE_IN_CODEX_REQUIRE_LIVE=1 but `claude` is not authenticated for the "
        f"login-backed modes the contract tests use ({detail}). Note that "
        "ANTHROPIC_API_KEY alone is NOT sufficient: config_mode=inherit and "
        "config_mode=safe deliberately strip it and require a Claude Code login "
        "session. A gate that cannot reach Anthropic verifies nothing."
    )


def _claude_help_advertises(flag: str) -> bool:
    try:
        proc = subprocess.run(
            ["claude", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in f"{proc.stdout}\n{proc.stderr}"


# `claude_consult` prompts below hand Claude real material to review rather than a
# verdict to affirm. A prompt like "reply that 2+2 equals 4 and give verdict pass"
# is verdict-setting, which the independent-critic guardrails correctly refuse; the
# refusal arrives as unstructured prose, and unstructured prose normalizes to
# verdict="unknown" with an empty everything-else. The old assertions accepted that,
# so they held whether the structured-output path worked or was never entered (#159).
_STRUCTURED_VERDICTS = ("pass", "concerns", "fail")

# One snippet per live consult, each with a real defect to find, so the answer has
# somewhere to put a verdict, a finding, and a recommendation. Each states the
# behavior the caller requires, which is what makes the defect a defect: without a
# contract to violate, "it depends what you want on empty input" is a legitimate
# answer, and legitimate uncertainty is exactly the response `_assert_structured`
# cannot tell apart from a parsing failure. Stating the requirement is not
# verdict-setting -- it says what the code must do, not what the reviewer must say.
_REVIEW_MATERIAL = (
    "Review this Python function for correctness and edge cases. Callers require it "
    "to return 0.0 for an empty list.\n\n"
    "def average(values):\n"
    "    return sum(values) / len(values)\n"
)
_SAFE_MODE_MATERIAL = (
    "Review this Python function for correctness and edge cases. Callers require it "
    "to return None for an empty file and to leave no file handle open.\n\n"
    "def last_line(path):\n"
    "    return open(path).readlines()[-1]\n"
)


def _assert_structured(data: dict) -> None:
    """Pin the SEMANTIC half of the envelope, which fixtures cannot cover.

    Two independent checks, because neither alone is sufficient:

    * The verdict is not `"unknown"`. That is what `normalize.py` emits when claude
      returned no structured JSON at all, so a parsing regression, a model that
      stopped emitting JSON, and a refused prompt all land there. It is a lossy
      signal in one direction only: a VALID structured reply may also carry
      `"unknown"` when the reviewer is genuinely uncertain. The prompts above give
      it firm ground so that stays rare, and the assertion message names both
      readings rather than asserting a parse failure it cannot prove.
    * At least one structured collection is populated. The unstructured path leaves
      findings, questions, assumptions and next_steps all empty and puts the whole
      reply in `summary`, so this fires on the same regression WITHOUT depending on
      the verdict value -- and it is the only check here that catches a
      parsed-but-empty envelope.
    """
    assert data["ok"] is True
    assert data["verdict"] in _STRUCTURED_VERDICTS, (
        f"verdict={data['verdict']!r}: either no structured JSON was parsed from "
        "claude's reply (a contract regression, or a refused prompt) or the reviewer "
        "was genuinely uncertain about material chosen to leave no room for it"
    )
    assert data["confidence"] in ("low", "medium", "high")
    assert data["summary"].strip(), "structured envelope carried an empty summary"
    assert any(data[key] for key in ("findings", "questions", "assumptions", "next_steps")), (
        "every structured collection was empty, which is what an unstructured reply "
        "normalizes to -- no structured JSON reached the envelope"
    )


async def test_status_live():
    async with Client(mcp) as client:
        result = await client.call_tool("claude_status", {})
    assert structured(result)["claude_found"] is True


async def test_ask_live_roundtrip():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_consult",
            {
                "prompt": _REVIEW_MATERIAL,
                "model": "haiku",
                "max_budget_usd": 0.20,
                "timeout_seconds": 120,
            },
        )
    data = structured(result)
    print("\n--- live claude_consult result ---")
    import json

    print(json.dumps(data, indent=2))
    _assert_structured(data)


@pytest.mark.skipif(
    not _claude_help_advertises("--safe-mode"),
    reason="installed claude CLI does not advertise --safe-mode",
)
async def test_ask_live_safe_mode_roundtrip():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_consult",
            {
                "prompt": _SAFE_MODE_MATERIAL,
                "config_mode": "safe",
                "model": "haiku",
                "max_budget_usd": 0.20,
                "timeout_seconds": 120,
            },
        )
    data = structured(result)
    print("\n--- live claude_consult safe-mode result ---")
    import json

    print(json.dumps(data, indent=2))
    _assert_structured(data)
    assert data["meta"]["config_mode"] == "safe"


async def test_consult_async_live_roundtrip(tmp_path, monkeypatch):
    """The live gate for #93's new path: a real detached `claude` run, end to end.

    The other tests here call the CLI in-process. This one is the only thing that
    exercises what the async starters actually added — `_launch_job` building
    argv, the job store spawning a detached worker, the prompt streaming to that
    worker's stdin, the child's envelope landing in the record, and
    claude_job_result rendering it back. AGENTS.md asks for a live run when
    Claude invocation changes; the pre-existing tests would have passed
    unchanged no matter what this PR did to the launch path.
    """
    import json
    import time

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_consult_async",
                {
                    "prompt": _REVIEW_MATERIAL,
                    "model": "haiku",
                    "max_budget_usd": 0.20,
                    "workspace_root": str(tmp_path),
                },
            )
        )
        assert started["ok"] is True
        assert started["status"] == "running"
        # The handle names the blocking tool whose envelope the result will carry.
        assert started["kind"] == "claude_consult"
        job_id = started["job_id"]

        deadline = time.time() + 180
        status = "running"
        while time.time() < deadline:
            status = structured(
                await client.call_tool(
                    "claude_job_status", {"job_id": job_id, "workspace_root": str(tmp_path)}
                )
            )["status"]
            if status != "running":
                break
            time.sleep(2)
        assert status == "done", f"job ended {status}, not done"

        data = structured(
            await client.call_tool(
                "claude_job_result", {"job_id": job_id, "workspace_root": str(tmp_path)}
            )
        )
    print("\n--- live claude_consult_async result ---")
    print(json.dumps(data, indent=2))
    _assert_structured(data)
    assert data["tool"] == "claude_consult"
    assert data["meta"]["job_id"] == job_id
    # A real detached run really did spend, which is what proves the child ran.
    assert data["meta"]["cost_usd"] > 0
