"""Contract-fingerprint guard.

`FINGERPRINT` (schemas.py) is bumped by hand, so nothing otherwise fails when the
agent-visible contract changes but the fingerprint is left stale. This test pins a
digest of that contract surface — the initialize serverInfo identity (minus the
release-tracking version), the full normalized tool records (names,
descriptions, titles, annotations, input/output schemas), resource and
resource-template records, prompt scaffolds, the capabilities payload (minus the
fingerprint/version fields themselves), the error-code catalog, the capability
summary, and `Meta`'s field names. Adding/removing/renaming a tool, changing a
schema or description or annotation, adding a `Meta` field, or editing the scope
text moves the digest and fails this test; an internal-only refactor does not.

When this test fails on an intentional contract change:
  1. bump FINGERPRINT in src/claude_in_codex/schemas.py (the `schema-NN` suffix), and
  2. update EXPECTED_CONTRACT_DIGEST below to the printed `actual` value.

Records are dumped `by_alias=True` so the digest covers the WIRE shape (the
camelCase keys and `_meta` a client actually receives), not the SDK's Python
field names — MCP SDK v2 renamed those to snake_case while keeping the wire
format, and a digest of field names would move on a framework upgrade that
changed nothing an agent can see. The FastMCP 3 -> 4 upgrade moved this digest
for that reason first (the by-alias surface was compared byte-for-byte against
the 3.4.7 build and found identical) and then for a real contract change: the
workspace_root descriptions now state that sessionless connections must pass it.
"""

import hashlib
import json
from typing import get_args

from tests.support import Client

from claude_in_codex import schemas
from claude_in_codex.server import CAPABILITY_SUMMARY, _capabilities_payload, mcp

EXPECTED_CONTRACT_DIGEST = "50194ed53d669d35eb544b62c5be2c9b34b792594a3fd8b9435dae47b101fd8f"


async def _contract_surface() -> dict:
    async with Client(mcp) as client:
        server_info = client.server_info.model_dump(mode="json", exclude_none=True, by_alias=True)
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()
    capabilities = _capabilities_payload()
    # Strip capabilities' own bump-tracked fields. Note this does NOT make the
    # digest independent of FINGERPRINT: the value is also a schema DEFAULT on
    # every result model, so it appears ~13 times in the advertised records and a
    # bump moves the digest on its own. That is harmless in the intended
    # workflow -- you bump because the contract changed, then re-pin -- but it
    # means a failure here does not prove the SHAPE moved, only that something
    # in this surface did. Stripping these two still earns its keep -- it keeps a
    # FINGERPRINT/version bump from moving the digest through the capabilities
    # payload as well -- but it does NOT make that payload shape-only: what
    # remains is mostly value-level contract TEXT (scope, negative_scope,
    # prerequisites, tool_details, annotations_policy, deprecation_policy), and
    # editing any of it moves the digest, which is the intent.
    capabilities.pop("fingerprint", None)
    capabilities.pop("version", None)
    # Same reason the capabilities version is stripped: serverInfo.version tracks
    # the release, so leaving it in would move the digest on every version bump and
    # make the fingerprint churn without a contract change. The identity fields
    # (name, and title/icons/websiteUrl once set) are contract shape.
    server_info.pop("version", None)
    return {
        "server_info": server_info,
        # Full normalized records: descriptions, titles, and annotations are part
        # of the agent-visible contract, not just the schemas. `meta` (FastMCP's
        # own {"fastmcp": {"tags": [...]}} block) is kept — verified identical
        # ({"fastmcp": {"tags": []}}) across every tool/resource in this server
        # and free of any fastmcp-version string, so it is stable contract shape,
        # not framework noise.
        "tools": {
            t.name: t.model_dump(mode="json", exclude_none=True, by_alias=True) for t in tools
        },
        "resources": {
            str(r.uri): r.model_dump(mode="json", exclude_none=True, by_alias=True)
            for r in resources
        },
        "resource_templates": {
            str(t.uri_template): t.model_dump(mode="json", exclude_none=True, by_alias=True)
            for t in templates
        },
        "prompts": {
            p.name: p.model_dump(mode="json", exclude_none=True, by_alias=True) for p in prompts
        },
        "capabilities": capabilities,
        "error_codes": sorted(get_args(schemas.ErrorCode)),
        "capability_summary": CAPABILITY_SUMMARY,
        # Meta's field names, digested DIRECTLY rather than only by way of the
        # advertised description that enumerates them (#143). `meta` is stubbed
        # in the tools' output schemas, so before this the only trace of a new
        # Meta field in the digest was that sentence -- which meant the gate
        # depended on an author remembering to edit prose, the same manual step
        # the fingerprint bump is. That description is now generated from these
        # names, so the two signals agree by construction; this one is kept
        # because it holds even if the description is ever hand-written again.
        "meta_fields": list(schemas.Meta.model_fields),
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
        "claude_consult",
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


async def test_contract_surface_covers_meta_field_names():
    """#143: the digest was blind to fields added to `Meta`.

    `meta` is advertised as an opaque object whose DESCRIPTION enumerates the
    field names, so the gate fired only if the author remembered to edit that
    prose -- the same manual step the fingerprint bump itself is, and this test
    exists precisely because manual steps get skipped. Probed on this branch
    before the fix: adding a field to `Meta` left all six tests green, while
    editing an advertised description failed them, so the instrument was live in
    general and blind to exactly this.

    Digesting the field names directly makes the coverage structural rather than
    a consequence of someone updating a sentence."""
    surface = await _contract_surface()

    assert surface["meta_fields"] == list(schemas.Meta.model_fields)
    # Not a tautology against an empty list: these are load-bearing names an
    # agent reads off the envelope.
    for name in ("cwd", "paths", "paths_matched", "cost_usd", "fingerprint"):
        assert name in surface["meta_fields"]


def test_advertised_meta_description_enumerates_every_meta_field():
    """The advertised enumeration must BE the field list, not a copy of it.

    Option (2) of #143: the sentence agents read is generated from
    `Meta.model_fields`, so it cannot drift from the model. This test pins that
    property against a future hand-written replacement -- if someone reverts the
    description to prose, it fails as soon as the two disagree."""
    described = schemas.meta_fields_from_description(schemas._META_STUB["description"])

    assert described == list(schemas.Meta.model_fields)


async def test_fingerprint_disclosure_names_the_meta_field_coverage():
    """The public disclosure must name what the digest actually pins.

    FINGERPRINT_COVERS is agent-readable and its own comment requires it to stay
    in sync with the pinned surface here. #143 added `Meta`'s field names to that
    surface, and the pre-existing "tool records ... input/output schemas" entry
    does not describe it -- `meta` is advertised as an opaque stub, which is the
    whole reason the blind spot existed."""
    covers = _capabilities_payload()["fingerprint_covers"]

    assert any("meta field names" in item for item in covers), covers
