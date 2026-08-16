"""Wire-size ratchet for the discovery surface.

The least-capable realistic client preloads every tools/list entry, so the
serialized catalog is a per-session token tax. FastMCP inlines $defs into every
entry, which is why the advertised schemas in schemas.py are slimmed (Meta
stubbed, ErrorInfo stubbed, pydantic titles stripped) before registration."""

import json

import pytest
from fastmcp import Client
from jsonschema import validate

from claude_in_codex.schemas import (
    DRY_RUN_SCHEMA,
    JOB_LIST_SCHEMA,
    JOB_STARTED_SCHEMA,
    JOB_STATUS_SCHEMA,
    RESULT_SCHEMA,
    STATUS_SCHEMA,
)
from claude_in_codex.server import mcp

# Measured 55,367 bytes, down from 63,970 before the error branch was compacted
# (and 113,495 before Meta was stubbed). Budgets carry ~2% headroom deliberately:
# an earlier ceiling sat 30 bytes above the payload, so unrelated metadata churn
# broke CI. Raising either budget is a reviewed, deliberate act — do not bump one
# to make a test pass.
#
# Raised from 53,000 for the bounded-`detail` contract (#94): +2,783 bytes / +5.3%
# over the 52,584 measured just before it. That buys the `truncation` block on
# every result union, a `detail` param on the two job-result tools (the free
# full-detail re-read), and the per-tool pointer to the caps. Kept to ~5% by
# publishing the contract ONCE in claude_capabilities.detail_modes and stubbing
# Truncation/DetailModes/OutputBounds in the advertised schemas, the same
# treatment Meta and ErrorInfo get above — spelling the caps out inline in all
# four paid tools measured ~2x this.
# Temporarily raised for the claude_ask/claude_review_dry_run deprecation window:
# each alias re-advertises its primary's full schemas (~9KB total). Revert to
# 56_300 when the aliases are removed in 0.9.0.
WIRE_BUDGET_BYTES = 66_000
# Deterministic, dependency-free stand-in for a real tokenizer. JSON schema text
# is ASCII-dense and packs ~4.13 bytes per o200k_base token, so ceil(bytes/4) is
# a conservative over-estimate — it read 12,964 against a measured 12,570 (+3.1%)
# at the previous ceiling — and never needs tiktoken in CI. The byte assertion
# stays authoritative; this one tracks the token budget issue #90 is written
# against, and is raised in step with WIRE_BUDGET_BYTES (ceil(56,300/4)).
TOKEN_PROXY_BUDGET = 16_500  # see WIRE_BUDGET_BYTES note; revert with it in 0.9.0


def _token_proxy(wire_bytes: int) -> int:
    return -(-wire_bytes // 4)


async def _tools_list_wire() -> tuple[str, list[dict]]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    payload = [t.model_dump(mode="json", exclude_none=True) for t in tools]
    return json.dumps(payload, separators=(",", ":")), payload


def _per_tool_report(payload: list[dict]) -> str:
    rows = sorted(
        (
            (len(json.dumps(t.get("outputSchema", {}), separators=(",", ":"))), t["name"])
            for t in payload
        ),
        reverse=True,
    )
    return "\n".join(f"    {name}: {size} outputSchema bytes" for size, name in rows)


async def test_tools_list_discovery_cost_within_budget():
    """One test, both budgets, asserted independently.

    The budgets are currently proportional (14,075 == 56,300/4) and the proxy is
    a pure function of the byte count, so neither can be busted alone today. They
    are still checked separately: tightening only TOKEN_PROXY_BUDGET later must
    actually enforce the tighter bound rather than be silently ignored."""
    wire, payload = await _tools_list_wire()
    proxy = _token_proxy(len(wire))
    detail = (
        f"tools/list discovery cost is {len(wire)} bytes / ~{proxy} proxy tokens "
        f"(budget {WIRE_BUDGET_BYTES} bytes / {TOKEN_PROXY_BUDGET} tokens); "
        "slim the advertised schemas instead of raising the budget.\n"
        f"{_per_tool_report(payload)}"
    )
    assert len(wire) <= WIRE_BUDGET_BYTES, detail
    assert proxy <= TOKEN_PROXY_BUDGET, detail


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


_UNION_SCHEMAS = {
    "RESULT_SCHEMA": RESULT_SCHEMA,
    "JOB_STARTED_SCHEMA": JOB_STARTED_SCHEMA,
    "JOB_STATUS_SCHEMA": JOB_STATUS_SCHEMA,
    "DRY_RUN_SCHEMA": DRY_RUN_SCHEMA,
    "JOB_LIST_SCHEMA": JOB_LIST_SCHEMA,
}


@pytest.mark.parametrize("name", sorted(_UNION_SCHEMAS))
def test_error_catalog_is_not_inlined_per_tool(name):
    """The 30-value ErrorCode enum must be published once, not per tool.

    It is the single largest repeated block in tools/list; embedding it in 11
    schemas is what put the catalog over the token budget in the first place."""
    wire = json.dumps(_UNION_SCHEMAS[name])
    assert "claude_auth_required" not in wire, (
        f"{name} inlines the error-code catalog; advertise the compact error "
        "branch and let claude_capabilities.error_codes publish the enum."
    )


def test_status_schema_does_not_inline_the_error_catalog():
    # StatusResult.default_errors is list[ErrorInfo], which drags the whole enum
    # into a schema that has no ErrorResult branch at all.
    assert "claude_auth_required" not in json.dumps(STATUS_SCHEMA)


@pytest.mark.parametrize("name", sorted(_UNION_SCHEMAS))
def test_advertised_schema_still_accepts_a_real_error_envelope(name):
    """The compact error branch must remain *conforming*, not merely absent.

    MCP says structured results conform to outputSchema, and the spec states no
    isError carve-out — so an ok:false envelope has to validate here even though
    the reference Python client happens to skip validation when isError is set.
    Dropping the error branch entirely would silently break a strict client."""
    envelope = {
        "ok": False,
        "error": {
            "code": "job_not_found",
            "message": "No job exists in this workspace.",
            "repair": "Call claude_job_list to see live jobs.",
            "retryable": False,
            "details": {"field": "job_id", "value": "abc", "reason": "unknown_or_expired"},
            "action": {
                "next_step": "call_tool",
                "tool": "claude_job_list",
                "arguments": {"workspace_root": "/repo"},
            },
        },
        "meta": {"cwd": "/repo", "elapsed_ms": 1, "fingerprint": "x"},
    }
    validate(envelope, _UNION_SCHEMAS[name])


async def test_live_error_envelope_validates_against_the_advertised_schema():
    """End-to-end counterpart to the hand-built envelope above.

    A real failing call must validate against the schema the server actually
    advertises for that tool, not just against the constant in schemas.py."""
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        result = await client.call_tool(
            "claude_job_status",
            {"job_id": "0" * 32, "workspace_root": "/nonexistent-workspace"},
            raise_on_error=False,
        )
    assert result.is_error, "expected a failing call to exercise the error branch"
    assert result.structured_content["ok"] is False
    validate(result.structured_content, tools["claude_job_status"].outputSchema)


async def test_invalid_arguments_envelope_validates_against_the_advertised_schema():
    """The middleware path builds a placeholder meta (no resolved cwd/config).

    It is the error envelope least like a normal one, so it is the one most
    likely to fall outside a compacted error branch."""
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        result = await client.call_tool(
            "claude_ask", {"prompt": "x", "detail": "verbose"}, raise_on_error=False
        )
    assert result.structured_content["error"]["code"] == "invalid_arguments"
    validate(result.structured_content, tools["claude_ask"].outputSchema)


async def test_capabilities_publishes_the_full_error_catalog():
    """The catalog left the per-tool schemas, so it must be reachable elsewhere."""
    from typing import get_args

    from claude_in_codex.schemas import CAPABILITIES_SCHEMA, ErrorCode

    async with Client(mcp) as client:
        result = await client.call_tool("claude_capabilities", {})
    assert set(result.structured_content["error_codes"]) == set(get_args(ErrorCode))
    # Required, not optional: this is the catalog's only machine-readable home,
    # so schema-driven clients must be able to rely on its presence.
    assert "error_codes" in CAPABILITIES_SCHEMA["required"]
