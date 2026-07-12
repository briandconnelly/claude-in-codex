"""Wire-size ratchet for the discovery surface.

The least-capable realistic client preloads every tools/list entry, so the
serialized catalog is a per-session token tax. FastMCP inlines $defs into every
entry, which is why the advertised schemas in schemas.py are slimmed (Meta
stubbed, pydantic titles stripped) before registration."""

import json

from fastmcp import Client

from claude_in_codex.schemas import RESULT_SCHEMA
from claude_in_codex.server import mcp

# Measured 62,642 bytes after slimming (baseline was 113,495); headroom for the
# additive fields from this remediation PR. Raising this budget is a reviewed,
# deliberate act — do not bump it to make a failing test pass.
WIRE_BUDGET_BYTES = 64_000


async def test_tools_list_wire_size_within_budget():
    async with Client(mcp) as client:
        tools = await client.list_tools()
    payload = [t.model_dump(mode="json", exclude_none=True) for t in tools]
    wire = json.dumps(payload, separators=(",", ":"))
    assert len(wire) <= WIRE_BUDGET_BYTES, (
        f"tools/list serialized to {len(wire)} bytes > {WIRE_BUDGET_BYTES} budget; "
        "slim the advertised schemas instead of raising the budget."
    )


def test_slim_keeps_property_named_title():
    # Finding.title is a real response field; the title-strip must not eat it.
    finding = RESULT_SCHEMA["$defs"]["Finding"]["properties"]
    assert "title" in finding


def test_slim_stubs_meta_as_open_object():
    meta = RESULT_SCHEMA["$defs"]["Meta"]
    assert meta["type"] == "object"
    assert "properties" not in meta
    assert "claude_capabilities" in meta["description"]


def test_slim_keeps_ok_discriminator():
    assert "ok" in RESULT_SCHEMA["properties"]
    assert RESULT_SCHEMA["required"] == ["ok"]
