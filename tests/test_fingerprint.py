"""Contract-fingerprint guard.

`FINGERPRINT` (schemas.py) is bumped by hand, so nothing otherwise fails when the
agent-visible contract changes but the fingerprint is left stale. This test pins a
digest of that contract surface — tool names, every tool's input/output schema, the
capabilities payload (minus the fingerprint/version fields themselves), the error-code
catalog, and the capability summary. Adding/removing/renaming a tool, changing a
schema, or editing the scope text moves the digest and fails this test; an
internal-only refactor does not.

When this test fails on an intentional contract change:
  1. bump FINGERPRINT in src/claude_in_codex/schemas.py (the `schema-NN` suffix), and
  2. update EXPECTED_CONTRACT_DIGEST below to the printed `actual` value.
"""

import hashlib
import json
from typing import get_args

from fastmcp import Client

from claude_in_codex import schemas
from claude_in_codex.server import CAPABILITY_SUMMARY, _capabilities_payload, mcp

EXPECTED_CONTRACT_DIGEST = "2a76864170820abb2700bc6c67445579b1608739fcb6f57bed06f7abcb3f818e"


async def _contract_surface() -> dict:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    tool_surface = {t.name: {"input": t.inputSchema, "output": t.outputSchema} for t in tools}
    capabilities = _capabilities_payload()
    # Strip the bump-tracked fields so the digest reflects contract SHAPE only;
    # otherwise bumping FINGERPRINT/version would circularly change the digest.
    capabilities.pop("fingerprint", None)
    capabilities.pop("version", None)
    return {
        "tools": tool_surface,
        "capabilities": capabilities,
        "error_codes": sorted(get_args(schemas.ErrorCode)),
        "capability_summary": CAPABILITY_SUMMARY,
    }


def _digest(surface: dict) -> str:
    return hashlib.sha256(json.dumps(surface, sort_keys=True, default=str).encode()).hexdigest()


async def test_contract_fingerprint_is_pinned():
    actual = _digest(await _contract_surface())
    assert actual == EXPECTED_CONTRACT_DIGEST, (
        "The agent-visible contract changed.\n"
        f"  expected: {EXPECTED_CONTRACT_DIGEST}\n"
        f"  actual:   {actual}\n"
        "If this change is intentional, bump FINGERPRINT in schemas.py (the "
        "schema-NN suffix) and set EXPECTED_CONTRACT_DIGEST to the actual value above."
    )


async def test_capabilities_payload_reports_current_fingerprint():
    assert _capabilities_payload()["fingerprint"] == schemas.FINGERPRINT


async def test_contract_digest_is_deterministic():
    assert _digest(await _contract_surface()) == _digest(await _contract_surface())
