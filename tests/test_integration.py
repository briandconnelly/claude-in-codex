"""Integration tests — require the real `claude` CLI (gated via skipif)."""

import shutil

import pytest
from fastmcp import Client

from cc_plugin_codex.server import mcp
from tests.conftest import structured

pytestmark = pytest.mark.skipif(shutil.which("claude") is None,
                                reason="claude CLI not installed")


async def test_status_live():
    async with Client(mcp) as client:
        result = await client.call_tool("claude_status", {})
    assert structured(result)["claude_found"] is True


async def test_ask_live_roundtrip():
    async with Client(mcp) as client:
        result = await client.call_tool("claude_ask", {
            "prompt": "Reply that 2+2 equals 4 and give verdict pass.",
            "model": "haiku", "max_budget_usd": 0.20, "timeout_seconds": 120,
        })
    data = structured(result)
    print("\n--- live claude_ask result ---")
    import json
    print(json.dumps(data, indent=2))
    assert data["ok"] is True
    assert data["verdict"] in ("pass", "concerns", "fail", "unknown")
