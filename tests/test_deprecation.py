"""The amicus deprecation (#196), read from the wire.

Every expectation here is written out literally rather than read back from
server._TOOL_SUCCESSORS: a test that derives its expectations from the map it
checks passes whatever the map says.
"""

from __future__ import annotations

import pytest
from tests.support import Client

from claude_in_codex import server
from claude_in_codex.server import CAPABILITY_SUMMARY, _capabilities_payload, mcp

LIFECYCLE_KEY = "dev.bconnelly.claude-in-codex/lifecycle"

# Checked against amicus's live tools/list on 2026-10-04.
EXPECTED_TOOL_SUCCESSORS = {
    "claude_consult": "amicus_consult",
    "claude_consult_async": "amicus_consult_async",
    "claude_review_changes": "amicus_review_changes",
    "claude_review_changes_async": "amicus_review_changes_async",
    "claude_adversarial_review": "amicus_adversarial_review",
    "claude_adversarial_review_async": "amicus_adversarial_review_async",
    "claude_dry_run": "amicus_review_changes_dry_run",
    "claude_models": "amicus_models",
    "claude_status": "amicus_backends",
    "claude_capabilities": "amicus_capabilities",
    "claude_job_status": "amicus_job_status",
    "claude_job_result": "amicus_job_result",
    "claude_job_consume_result": "amicus_job_consume_result",
    "claude_job_cancel": "amicus_job_cancel",
    "claude_job_list": "amicus_job_list",
}
# The amicus successors whose inputSchema lists `backend` as required.
REQUIRES_BACKEND = {
    "amicus_consult",
    "amicus_consult_async",
    "amicus_review_changes",
    "amicus_review_changes_async",
    "amicus_adversarial_review",
    "amicus_adversarial_review_async",
    "amicus_review_changes_dry_run",
    "amicus_models",
}
PAID_TOOLS = {
    "claude_consult",
    "claude_consult_async",
    "claude_review_changes",
    "claude_review_changes_async",
    "claude_adversarial_review",
    "claude_adversarial_review_async",
}
JOB_TOOLS = {
    "claude_job_status",
    "claude_job_result",
    "claude_job_consume_result",
    "claude_job_cancel",
    "claude_job_list",
}
EXPECTED_RESOURCE_SUCCESSORS = {
    "claude-in-codex://models": "amicus://models/claude",
    "claude://models": "amicus://models/claude",
    "claude-in-codex://capabilities": "amicus://capabilities",
}
MARKER_FIELDS = {"since", "removal_at_or_after", "replaced_by", "migration"}
CLOSED_TIERS = {"stable", "preview", "experimental"}


async def _wire():
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        resources = {str(r.uri): r for r in await client.list_resources()}
    return tools, resources


def _lifecycle(record) -> dict:
    return record.model_dump(mode="json", by_alias=True)["_meta"][LIFECYCLE_KEY]


async def test_every_tool_carries_a_lifecycle_marker_naming_its_successor():
    tools, _ = await _wire()
    assert set(tools) == set(EXPECTED_TOOL_SUCCESSORS)
    for name, tool in tools.items():
        lifecycle = _lifecycle(tool)
        assert set(lifecycle) == {"stability", "deprecation"}, name
        # Closed tier set: the deprecation is a separate axis, never a tier value.
        assert lifecycle["stability"] in CLOSED_TIERS, name
        marker = lifecycle["deprecation"]
        assert set(marker) == MARKER_FIELDS, name
        assert marker["since"] == "0.10.0", name
        assert marker["removal_at_or_after"] == "0.11.0", name
        assert marker["replaced_by"] == EXPECTED_TOOL_SUCCESSORS[name], name
        assert marker["migration"], name


async def test_lifecycle_stability_matches_the_server_tier():
    tools, _ = await _wire()
    tier = _capabilities_payload()["stability"]
    assert tier in CLOSED_TIERS
    assert {_lifecycle(t)["stability"] for t in tools.values()} == {tier}


async def test_every_description_opens_with_its_successor():
    tools, _ = await _wire()
    for name, tool in tools.items():
        prefix = f"Deprecated: use {EXPECTED_TOOL_SUCCESSORS[name]}. "
        assert (tool.description or "").startswith(prefix), name


async def test_every_resource_carries_a_marker_naming_its_successor_uri():
    _, resources = await _wire()
    assert set(resources) == set(EXPECTED_RESOURCE_SUCCESSORS)
    for uri, resource in resources.items():
        lifecycle = _lifecycle(resource)
        assert lifecycle["stability"] in CLOSED_TIERS, uri
        marker = lifecycle["deprecation"]
        assert set(marker) == MARKER_FIELDS, uri
        assert marker["since"] == "0.10.0", uri
        assert marker["removal_at_or_after"] == "0.11.0", uri
        assert marker["replaced_by"] == EXPECTED_RESOURCE_SUCCESSORS[uri], uri
        successor = EXPECTED_RESOURCE_SUCCESSORS[uri]
        assert (resource.description or "").startswith(f"Deprecated: use {successor}. "), uri


async def test_migration_names_backend_only_where_amicus_requires_it():
    tools, _ = await _wire()
    for name, tool in tools.items():
        migration = _lifecycle(tool)["deprecation"]["migration"]
        needs_backend = EXPECTED_TOOL_SUCCESSORS[name] in REQUIRES_BACKEND
        assert ('backend="claude"' in migration) is needs_backend, name
        # The successor's name rides `replaced_by`; the prose must not restate it.
        assert EXPECTED_TOOL_SUCCESSORS[name] not in migration, name


async def test_paid_migration_maps_the_claude_knobs_to_backend_options():
    tools, _ = await _wire()
    for name in PAID_TOOLS:
        migration = _lifecycle(tools[name])["deprecation"]["migration"]
        for knob in ("access", "config_mode", "max_budget_usd", "CLAUDE_IN_CODEX_*"):
            assert knob in migration, (name, knob)
        assert "backend_options" in migration, name
    dry_run = _lifecycle(tools["claude_dry_run"])["deprecation"]["migration"]
    assert "config_mode" in dry_run and "backend_options" in dry_run


async def test_job_migration_points_at_this_servers_job_tools():
    # Status and list cannot finish a job, so the shared text must not say
    # "with this tool".
    tools, _ = await _wire()
    for name in JOB_TOOLS:
        migration = _lifecycle(tools[name])["deprecation"]["migration"]
        assert "Job ids do not carry over" in migration, name
        assert "this server's job tools" in migration, name
        assert "this tool" not in migration.replace("this server's job tools", ""), name


async def test_capabilities_repeat_each_tools_wire_marker():
    tools, _ = await _wire()
    async with Client(mcp) as client:
        data = (await client.call_tool("claude_capabilities", {})).structured_content
    details = {entry["name"]: entry for entry in data["tool_details"]}
    # Every wire tool, claude_capabilities included: the docs promise each one.
    assert set(details) == set(tools)
    for name, entry in details.items():
        assert entry["deprecation"] == _lifecycle(tools[name])["deprecation"], name


def test_capabilities_keep_a_null_replaced_by(monkeypatch):
    # The marker's field set is fixed, so exclude_none must not strip a null
    # successor. No tool has one today; force it to prove the payload keeps it.
    marker = server._TOOL_DEPRECATIONS["claude_models"].model_copy(update={"replaced_by": None})
    monkeypatch.setitem(server._TOOL_DEPRECATIONS, "claude_models", marker)
    details = {e["name"]: e for e in _capabilities_payload()["tool_details"]}
    assert "replaced_by" in details["claude_models"]["deprecation"]
    assert details["claude_models"]["deprecation"]["replaced_by"] is None
    assert server._deprecated_doc(marker, "Body.") == (
        "Deprecated: amicus has no equivalent. Body."
    )


def test_an_unmapped_tool_fails_at_registration():
    def claude_unmapped() -> None:
        """Not in the successor map."""

    with pytest.raises(KeyError):
        server._deprecated_tool()(claude_unmapped)


def test_instructions_open_with_the_deprecation_notice():
    # Read from the server, not Client.initialize_result: that is None in-process.
    assert mcp.instructions == CAPABILITY_SUMMARY
    assert CAPABILITY_SUMMARY.startswith("DEPRECATED: superseded by amicus (")
    notice = CAPABILITY_SUMMARY[: CAPABILITY_SUMMARY.index("RULES.")]
    assert "https://github.com/briandconnelly/amicus" in notice
    assert "When both are installed, prefer amicus's tools." in notice
    assert "0.10.0 is the final release" in notice
    assert "archived" in notice
    assert "keep running but get no fixes" in notice
    assert "or null where amicus has none" in notice


async def test_capabilities_docstring_discloses_the_marker():
    tools, _ = await _wire()
    assert "deprecation marker" in (tools["claude_capabilities"].description or "")
