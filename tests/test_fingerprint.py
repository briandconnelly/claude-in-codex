"""Contract-fingerprint guard.

`FINGERPRINT` (schemas.py) is bumped by hand, so nothing otherwise fails when the
agent-visible contract changes but the fingerprint is left stale. This test pins a
digest of that contract surface — the full normalized tool records (names,
descriptions, titles, annotations, input/output schemas), resource and
resource-template records, prompt scaffolds, the capabilities payload (minus the
fingerprint/version fields themselves), the error-code catalog, and the capability
summary. Adding/removing/renaming a tool, changing a schema or description or
annotation, or editing the scope text moves the digest and fails this test; an
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

EXPECTED_CONTRACT_DIGEST = "10fb56681fbd0ebbc62718fc81f89bc97cad0d7229b7d672ecfa77dc461a6dba"


async def _contract_surface() -> dict:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()
    capabilities = _capabilities_payload()
    # Strip the bump-tracked fields so the digest reflects contract SHAPE only;
    # otherwise bumping FINGERPRINT/version would circularly change the digest.
    capabilities.pop("fingerprint", None)
    capabilities.pop("version", None)
    return {
        # Full normalized records: descriptions, titles, and annotations are part
        # of the agent-visible contract, not just the schemas. `meta` (FastMCP's
        # own {"fastmcp": {"tags": [...]}} block) is kept — verified identical
        # ({"fastmcp": {"tags": []}}) across every tool/resource in this server
        # and free of any fastmcp-version string, so it is stable contract shape,
        # not framework noise.
        "tools": {t.name: t.model_dump(mode="json", exclude_none=True) for t in tools},
        "resources": {str(r.uri): r.model_dump(mode="json", exclude_none=True) for r in resources},
        "resource_templates": {
            str(t.uriTemplate): t.model_dump(mode="json", exclude_none=True) for t in templates
        },
        "prompts": {p.name: p.model_dump(mode="json", exclude_none=True) for p in prompts},
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


async def test_capabilities_payload_discloses_fingerprint_coverage():
    covers = _capabilities_payload()["fingerprint_covers"]
    assert covers, "fingerprint_covers must be a non-empty list"
    assert any("resource" in item for item in covers)


async def test_contract_surface_includes_resources():
    surface = await _contract_surface()
    assert "claude-in-codex://models" in surface["resources"]
    assert "claude-in-codex://capabilities" in surface["resources"]


async def test_contract_surface_pins_annotations_and_descriptions():
    surface = await _contract_surface()
    for name in (
        "claude_ask",
        "claude_review_changes",
        "claude_adversarial_review",
        "claude_review_changes_async",
    ):
        paid_tool = surface["tools"][name]
        assert paid_tool["annotations"]["readOnlyHint"] is False
        assert paid_tool["annotations"]["destructiveHint"] is True
        description = " ".join(paid_tool["description"].lower().split())
        assert "workspace hooks may run shell" in description
    models = surface["resources"]["claude-in-codex://models"]
    assert "description" in models
    assert "resource_templates" in surface
    assert "prompts" in surface
