"""Integration tests — require the real `claude` CLI (gated via skipif)."""

import shutil
import subprocess

import pytest
from fastmcp import Client
from tests.conftest import structured

from cc_plugin_codex.server import mcp

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


async def test_status_live():
    async with Client(mcp) as client:
        result = await client.call_tool("claude_status", {})
    assert structured(result)["claude_found"] is True


async def test_ask_live_roundtrip():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_ask",
            {
                "prompt": "Reply that 2+2 equals 4 and give verdict pass.",
                "model": "haiku",
                "max_budget_usd": 0.20,
                "timeout_seconds": 120,
            },
        )
    data = structured(result)
    print("\n--- live claude_ask result ---")
    import json

    print(json.dumps(data, indent=2))
    assert data["ok"] is True
    assert data["verdict"] in ("pass", "concerns", "fail", "unknown")


@pytest.mark.skipif(
    not _claude_help_advertises("--safe-mode"),
    reason="installed claude CLI does not advertise --safe-mode",
)
async def test_ask_live_safe_mode_roundtrip():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_ask",
            {
                "prompt": "Reply that safe mode works and give verdict pass.",
                "config_mode": "safe",
                "model": "haiku",
                "max_budget_usd": 0.20,
                "timeout_seconds": 120,
            },
        )
    data = structured(result)
    print("\n--- live claude_ask safe-mode result ---")
    import json

    print(json.dumps(data, indent=2))
    assert data["ok"] is True
    assert data["meta"]["config_mode"] == "safe"
    assert data["verdict"] in ("pass", "concerns", "fail", "unknown")
