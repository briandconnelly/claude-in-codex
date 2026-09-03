"""Integration tests — require the real `claude` CLI (gated via skipif)."""

import shutil
import subprocess

import pytest
from tests.conftest import structured
from tests.support import Client

from claude_in_codex.server import mcp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed"),
]


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
# somewhere to put a verdict, a finding, and a recommendation.
_REVIEW_MATERIAL = (
    "Review this Python function for correctness and edge cases:\n\n"
    "def average(values):\n"
    "    return sum(values) / len(values)\n"
)
_SAFE_MODE_MATERIAL = (
    "Review this Python function for correctness and edge cases:\n\n"
    "def last_line(path):\n"
    "    return open(path).readlines()[-1]\n"
)


def _assert_structured(data: dict) -> None:
    """Pin the SEMANTIC half of the envelope, which fixtures cannot cover.

    `verdict="unknown"` is what `normalize.py` emits when claude returned no
    structured JSON at all, so excluding it is the whole assertion: a regression in
    structured parsing, a model that stopped emitting JSON, and a refused prompt all
    land there and all must fail here. The populated-field checks catch the narrower
    case of a parsed-but-empty envelope.
    """
    assert data["ok"] is True
    assert data["verdict"] in _STRUCTURED_VERDICTS, (
        f"verdict={data['verdict']!r}: no structured JSON was parsed from claude's "
        "reply, so the structured-output contract went unexercised"
    )
    assert data["confidence"] in ("low", "medium", "high")
    assert data["summary"].strip(), "structured envelope carried an empty summary"


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
