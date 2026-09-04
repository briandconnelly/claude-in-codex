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

import pydantic
from tests.support import Client

from claude_in_codex import schemas
from claude_in_codex.server import CAPABILITY_SUMMARY, _capabilities_payload, mcp

EXPECTED_CONTRACT_DIGEST = "4e25a01349ea6c1143317d1e4288e457b915aac6ab486ed60d0267de022891de"


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


async def test_adding_a_meta_field_moves_the_digest():
    """The premise of #179, made executable instead of merely read.

    #179 removed the advertised `meta` enumeration on the grounds that #143 had
    ALSO started digesting Meta's field names directly, so the digest would still
    move when a field is added. Everything in that sentence was verified by
    READING -- FINGERPRINT_COVERS says it, `_contract_surface` looks like it does
    it. Neither is a demonstration, and a coverage claim nobody has watched fail
    is exactly the kind of instrument that turns out to have been broken all
    along. #143's own docstring records that the pre-fix gate was green while
    blind, so this project has already been bitten by trusting this by inspection.

    Probe it: add a field to Meta and assert the digest actually moves. If a
    refactor ever unhooks meta_fields from the surface, this fails and #179's
    justification fails with it -- rather than the two silently parting ways."""
    before = _digest(await _contract_surface())

    field = ("probe_field_that_is_not_in_meta", (str | None, None))
    probed = pydantic.create_model("MetaWithProbe", __base__=schemas.Meta, **{field[0]: field[1]})
    original = schemas.Meta
    try:
        schemas.Meta = probed
        after = _digest(await _contract_surface())
    finally:
        schemas.Meta = original

    assert after != before, (
        "Adding a field to Meta did NOT move the contract digest. The direct "
        "meta_fields coverage (#143) is what #179 relied on when it removed the "
        "advertised enumeration; if this fails, that removal is no longer safe."
    )
    # The instrument is restored, not just claimed to be.
    assert _digest(await _contract_surface()) == before


def test_capabilities_publishes_every_meta_field():
    """The published enumeration must BE the field list, not a copy of it.

    #143 made the advertised `meta` description a generated enumeration so it
    could not drift from `Meta`. #179 moved that enumeration out of tools/list --
    it was 14 identical copies, 8.9% of the discovery payload -- and into the
    capabilities payload, which is where the `meta` stub's description now points.

    The anti-drift property has to move with it, or the move traded 6,818 bytes
    for a contract an author can silently let rot. This is that property,
    asserted at the new home."""
    assert _capabilities_payload()["meta_fields"] == list(schemas.Meta.model_fields)
    # Not vacuous against an empty list: these are load-bearing names an agent
    # reads off the envelope.
    for name in ("cwd", "paths", "paths_matched", "cost_usd", "fingerprint"):
        assert name in _capabilities_payload()["meta_fields"]


def test_advertised_meta_stub_does_not_re_inline_the_field_list():
    """The 6,818 bytes #179 recovered must stay recovered.

    The stub is inlined into every tool's output schema, so any enumeration here
    is billed 14x on every tools/list. A future author restoring "helpful" detail
    would undo the recovery silently -- tests/test_discovery_cost.py would catch
    the total, but only after it had eaten the headroom this bought.

    The stub must point at the field list, not repeat it."""
    description = schemas._META_STUB["description"]

    assert "claude_capabilities" in description
    assert len(description) < 100, description
    # The check that actually binds: no Meta field name may appear. Probed
    # against the pre-#179 description, which named all 25 and fails this.
    named = [f for f in schemas.Meta.model_fields if f in description]
    assert not named, f"meta stub re-inlines field names: {named}"


async def test_fingerprint_disclosure_names_the_meta_field_coverage():
    """The public disclosure must name what the digest actually pins.

    FINGERPRINT_COVERS is agent-readable and its own comment requires it to stay
    in sync with the pinned surface here. #143 added `Meta`'s field names to that
    surface, and the pre-existing "tool records ... input/output schemas" entry
    does not describe it -- `meta` is advertised as an opaque stub, which is the
    whole reason the blind spot existed."""
    covers = _capabilities_payload()["fingerprint_covers"]

    assert any("meta field names" in item for item in covers), covers
