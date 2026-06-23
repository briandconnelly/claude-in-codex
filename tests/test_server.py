import json
import types

import anyio
import pytest
from fastmcp import Client
from tests.conftest import structured

from claude_in_codex.cli_contract import ALWAYS_SEND_FLAGS, HELP_GATED_FLAGS
from claude_in_codex.preflight import FlagSupport
from claude_in_codex.server import (
    CAPABILITY_SUMMARY,
    _capabilities_payload,
    _first_root,
    _resolve_workspace,
    mcp,
)

PAID_TOOLS = ("claude_ask", "claude_review_changes", "claude_adversarial_review")


def _patch_full_flag_support(monkeypatch):
    """Make claude_status' --help probe deterministic: every expected flag present,
    so flags_warning stays None and no real `claude --help` runs."""
    import claude_in_codex.server as srv

    fs = FlagSupport(
        supported=frozenset(ALWAYS_SEND_FLAGS).union(HELP_GATED_FLAGS), help_parsed=True
    )
    monkeypatch.setattr(srv.preflight, "flag_support", lambda *a, **k: fs)


class _FakeRoots:
    """Minimal stand-in for a FastMCP Context exposing list_roots()."""

    def __init__(self, uris=None, raises=False):
        self._uris = uris or []
        self._raises = raises

    async def list_roots(self):
        if self._raises:
            raise RuntimeError("client does not support roots")
        return [type("R", (), {"uri": u})() for u in self._uris]


async def test_first_root_returns_path_from_file_uri():
    ctx = _FakeRoots(["file:///home/me/project"])
    assert await _first_root(ctx) == "/home/me/project"


async def test_first_root_none_when_unsupported():
    assert await _first_root(_FakeRoots(raises=True)) is None


async def test_first_root_skips_non_file_uris():
    ctx = _FakeRoots(["https://example.com/x", "file:///ok"])
    assert await _first_root(ctx) == "/ok"


async def test_resolve_workspace_param_inside_root_beats_root_default(tmp_path):
    child = tmp_path / "repo"
    child.mkdir()
    ctx = _FakeRoots([tmp_path.as_uri()])
    path, err, source = await _resolve_workspace(str(child), ctx)
    assert err is None
    assert path == str(child)
    assert source == "param"


async def test_resolve_workspace_uses_roots_when_no_param(tmp_path):
    ctx = _FakeRoots([tmp_path.as_uri()])
    path, err, source = await _resolve_workspace(None, ctx)
    assert err is None
    assert path == str(tmp_path)
    assert source == "roots"


async def test_resolve_workspace_param_must_be_inside_roots(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = _FakeRoots([root.as_uri()])
    path, err, source = await _resolve_workspace(str(outside), ctx)
    assert path is None
    assert err == "workspace_outside_roots"
    assert source is None


async def test_resolve_workspace_param_inside_roots_allowed(tmp_path):
    root = tmp_path / "root"
    child = root / "repo"
    child.mkdir(parents=True)
    ctx = _FakeRoots([root.as_uri()])
    path, err, source = await _resolve_workspace(str(child), ctx)
    assert err is None
    assert path == str(child)
    assert source == "param"


async def test_resolve_workspace_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path, err, source = await _resolve_workspace(None, _FakeRoots(raises=True))
    assert err is None
    assert path == str(tmp_path)
    assert source == "cwd"


async def test_resolve_workspace_rejects_nonexistent_param():
    path, err, source = await _resolve_workspace("/no/such/dir/xyz", _FakeRoots())
    assert path is None
    assert err == "invalid_workspace_root"


async def test_resolve_workspace_rejects_relative_param(tmp_path, monkeypatch):
    # A relative workspace_root must be rejected — it would resolve against the
    # untrusted cwd that workspace resolution exists to bypass.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    path, err, source = await _resolve_workspace("sub", _FakeRoots())
    assert path is None
    assert err == "invalid_workspace_root"


async def _tools_by_name():
    async with Client(mcp) as client:
        return {t.name: t for t in await client.list_tools()}


async def test_list_tools():
    names = set(await _tools_by_name())
    assert {
        "claude_ask",
        "claude_review_changes",
        "claude_adversarial_review",
        "claude_status",
    } <= names


async def test_tools_publish_real_output_schema():
    # F1: the ok-discriminated contract must be in the schema, not just prose.
    tools = await _tools_by_name()
    for name in (*PAID_TOOLS, "claude_status"):
        schema = tools[name].outputSchema
        assert schema is not None
        assert schema != {"additionalProperties": True, "type": "object"}, name
        assert schema.get("type") == "object", name
        assert '"ok"' in json.dumps(schema), name


async def test_paid_tool_output_schema_describes_both_outcomes():
    # F1: success and error shapes are both discoverable from the schema.
    schema = (await _tools_by_name())["claude_ask"].outputSchema
    blob = json.dumps(schema)
    assert "summary" in blob and "verdict" in blob  # success branch
    assert "error" in blob and "repair" in blob  # error branch


async def test_fixed_value_inputs_use_enums():
    # F2: choices are JSON Schema enums, not prose like "inherit|scoped|safe|bare".
    props = (await _tools_by_name())["claude_review_changes"].inputSchema["properties"]
    dry_props = (await _tools_by_name())["claude_review_dry_run"].inputSchema["properties"]
    assert props["scope"]["enum"] == ["working_tree", "staged", "branch"]
    assert dry_props["scope"]["enum"] == ["working_tree", "staged", "branch"]
    assert props["detail"]["enum"] == ["summary", "full"]

    def _enum_in_anyof(prop):
        for branch in prop.get("anyOf", []):
            if "enum" in branch:
                return branch["enum"]
        return prop.get("enum")

    assert _enum_in_anyof(props["config_mode"]) == ["inherit", "scoped", "safe", "bare"]
    assert _enum_in_anyof(props["access"]) == ["toolless", "readonly"]
    assert _enum_in_anyof(dry_props["config_mode"]) == ["inherit", "scoped", "safe", "bare"]


async def test_tools_have_titles():
    # F8: human-facing title for mixed human/agent pickers.
    tools = await _tools_by_name()
    for name in (*PAID_TOOLS, "claude_status"):
        assert tools[name].title, name


async def test_capability_summary_declares_tier_and_blocking():
    # F9 stability tier + F4 blocking/cancel disclosure.
    summary = CAPABILITY_SUMMARY.lower()
    assert "experimental" in summary
    assert "cancel" in summary
    assert len(CAPABILITY_SUMMARY) < 900


async def test_tool_descriptions_are_concise_and_disambiguating():
    tools = await _tools_by_name()
    for tool in tools.values():
        assert len(tool.description or "") <= 450, tool.name
    assert "question or design choice" in tools["claude_ask"].description
    assert "git diff" in tools["claude_review_changes"].description
    assert "background" in tools["claude_review_changes_async"].description
    assert "without deleting" in tools["claude_job_result"].description
    assert "delete the stored job record" in tools["claude_job_consume_result"].description


async def test_common_optional_params_are_described():
    tools = await _tools_by_name()
    for name in ("claude_ask", "claude_review_changes", "claude_adversarial_review"):
        props = tools[name].inputSchema["properties"]
        assert props["model"]["description"]
        assert props["max_budget_usd"]["description"]
        assert props["timeout_seconds"]["description"]
    assert tools["claude_adversarial_review"].inputSchema["properties"]["base"]["description"]
    for name in (
        "claude_review_changes",
        "claude_review_changes_async",
        "claude_adversarial_review",
        "claude_review_dry_run",
    ):
        assert tools[name].inputSchema["properties"]["paths"]["description"]


async def test_status_reports_config_modes(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        result = await client.call_tool("claude_status", {})
    data = structured(result)
    assert "config_modes_available" in data
    assert data["config_modes_available"]["safe"] is True
    assert data["config_modes_available"]["bare"] is False
    assert data["hooks_disabled"] is False
    assert "$0.10-$0.20" in data["resolved_defaults"]["practical_min_budget_hint"]


async def test_status_does_not_claim_hooks_disabled_when_bare_unavailable(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bare")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["ready"] is False
    assert data["resolved_defaults"]["config_mode"] == "bare"
    assert data["config_modes_available"]["bare"] is False
    assert data["hooks_disabled"] is False
    assert data["default_errors"][0]["code"] == "api_key_missing"


async def test_status_claims_hooks_disabled_for_safe_without_api_key(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "safe")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["resolved_defaults"]["config_mode"] == "safe"
    assert data["config_modes_available"]["safe"] is True
    assert data["hooks_disabled"] is True


async def test_status_does_not_claim_safe_available_when_help_omits_flag(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")

    class _Ver:
        stdout = "2.0.0 (Claude Code)"

    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: _Ver())
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    supported = frozenset(ALWAYS_SEND_FLAGS).union(HELP_GATED_FLAGS) - frozenset({"--safe-mode"})
    monkeypatch.setattr(
        srv.preflight,
        "flag_support",
        lambda *a, **k: FlagSupport(supported=supported, help_parsed=True),
    )
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "safe")
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["ready"] is False
    assert data["config_modes_available"]["safe"] is False
    assert data["hooks_disabled"] is False
    assert data["default_errors"][0]["code"] == "unsupported_config_mode"
    assert "--safe-mode" in data["flags_warning"]


async def test_status_does_not_claim_safe_available_when_claude_missing(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: None)
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "safe")
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["claude_found"] is False
    assert data["config_modes_available"]["inherit"] is False
    assert data["config_modes_available"]["scoped"] is False
    assert data["config_modes_available"]["safe"] is False
    assert data["config_modes_available"]["bare"] is False
    assert data["hooks_disabled"] is False


async def test_status_reports_cli_missing_before_invalid_defaults(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: None)
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bogus")
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["ready"] is False
    assert "CLI was not found" in data["readiness_detail"]
    assert data["default_errors"][0]["code"] == "unsupported_config_mode"


async def test_status_reports_invalid_env_defaults(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bogus")
    monkeypatch.setenv("CLAUDE_IN_CODEX_ACCESS", "sideways")
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["ready"] is False
    assert "default configuration is invalid" in data["readiness_detail"]
    assert data["claude_authenticated"] is True
    assert data["raw_defaults"]["config_mode"] == "bogus"
    assert data["raw_defaults"]["access"] == "sideways"
    assert data["resolved_defaults"]["config_mode"] == "inherit"
    assert data["resolved_defaults"]["access"] == "toolless"
    assert {err["code"] for err in data["default_errors"]} == {
        "unsupported_config_mode",
        "unsupported_access",
    }


async def test_status_flags_unexpanded_env_placeholders(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    # Host delivered literal ${...} for both a config knob and the API key.
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "${CLAUDE_IN_CODEX_CLAUDE_CONFIG}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "${ANTHROPIC_API_KEY}")
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["ready"] is False
    codes = [err["code"] for err in data["default_errors"]]
    # The placeholder diagnostic fires...
    assert "unexpanded_env_placeholder" in codes
    placeholder = next(
        e for e in data["default_errors"] if e["code"] == "unexpanded_env_placeholder"
    )
    assert "CLAUDE_IN_CODEX_CLAUDE_CONFIG" in placeholder["message"]
    # ...and names the non-empty API key, which would otherwise look valid.
    assert "ANTHROPIC_API_KEY" in placeholder["message"]
    # The misleading per-knob "Unknown config_mode '${...}'" error is suppressed.
    assert "unsupported_config_mode" not in codes


async def test_status_no_placeholder_error_for_valid_env(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "scoped")
    monkeypatch.setenv("CLAUDE_IN_CODEX_ACCESS", "readonly")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    codes = [err["code"] for err in data["default_errors"]]
    assert "unexpanded_env_placeholder" not in codes


async def test_status_warns_api_key_set_in_login_mode(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "inherit")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["api_key_present"] is True
    assert "ignored in config_mode inherit/scoped/safe" in data["api_key_warning"]
    assert "config_mode=bare" in data["api_key_warning"]
    # The key value must never appear in any output field.
    assert "sk-ant-secret-value" not in json.dumps(data)


async def test_status_no_api_key_warning_in_bare_mode(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bare")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    # bare mode deliberately uses the key, so its presence is not a caveat.
    assert data["api_key_present"] is True
    assert "api_key_warning" not in data
    assert "sk-ant-secret-value" not in json.dumps(data)


async def test_status_no_api_key_warning_when_unset(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "inherit")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["api_key_present"] is False
    assert "api_key_warning" not in data


async def test_status_no_api_key_warning_for_placeholder_in_login_mode(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "inherit")
    # A literal ${...} is non-empty (present) but is diagnosed by the placeholder
    # default_error path, so the override warning must not duplicate it.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "${ANTHROPIC_API_KEY}")
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["api_key_present"] is True
    assert "api_key_warning" not in data
    codes = [err["code"] for err in data["default_errors"]]
    assert "unexpanded_env_placeholder" in codes


async def test_safe_mode_rejected_before_paid_call_when_help_omits_flag(
    fake_claude, monkeypatch, tmp_path
):
    import claude_in_codex.server as srv

    async def fail_run(*args, **kwargs):
        raise AssertionError("paid call should not run")

    supported = frozenset(ALWAYS_SEND_FLAGS).union(HELP_GATED_FLAGS) - frozenset({"--safe-mode"})
    monkeypatch.setattr(
        srv.preflight,
        "flag_support",
        lambda *a, **k: FlagSupport(supported=supported, help_parsed=True),
    )
    monkeypatch.setattr(srv, "run_claude_async", fail_run)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_ask",
            {"prompt": "x", "config_mode": "safe", "workspace_root": str(tmp_path)},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "unsupported_config_mode"
    assert "--safe-mode" in data["error"]["message"]


async def test_claude_ask_returns_normalized(fake_claude):
    async with Client(mcp) as client:
        result = await client.call_tool("claude_ask", {"prompt": "is this safe?"})
    data = structured(result)
    assert data["ok"] is True
    assert data["verdict"] == "concerns"
    assert data["meta"]["fingerprint"] == "claude-in-codex/0.1/schema-25"


async def test_claude_ask_rejects_oversized_prompt_before_paid_call(monkeypatch, tmp_path):
    import claude_in_codex.server as srv

    async def fail_run(*args, **kwargs):
        raise AssertionError("paid call should not run")

    monkeypatch.setenv("CLAUDE_IN_CODEX_MAX_INPUT_BYTES", "1000")
    monkeypatch.setattr(srv, "run_claude_async", fail_run)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_ask",
            {"prompt": "x" * 1500, "workspace_root": str(tmp_path)},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "context_too_large"
    assert data["error"]["offending_param"] == "prompt"


async def test_adversarial_rejects_oversized_evidence_before_paid_call(monkeypatch, tmp_path):
    import claude_in_codex.server as srv

    async def fail_run(*args, **kwargs):
        raise AssertionError("paid call should not run")

    monkeypatch.setenv("CLAUDE_IN_CODEX_MAX_INPUT_BYTES", "1000")
    monkeypatch.setattr(srv, "run_claude_async", fail_run)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_adversarial_review",
            {"target": "x", "evidence": "y" * 1500, "workspace_root": str(tmp_path)},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "context_too_large"
    assert data["error"]["offending_param"] == "evidence"


async def test_invalid_enum_param_rejected_by_schema(fake_claude):
    # F2: invalid enum values are rejected at the schema boundary (clients can
    # validate locally) rather than round-tripping to a structured error.
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc:
            await client.call_tool("claude_ask", {"prompt": "x", "config_mode": "bogus"})
    assert "inherit" in str(exc.value)


async def test_bogus_env_config_mode_is_structured_error(fake_claude, monkeypatch):
    # The structured unsupported_config_mode path is still reachable via a bad
    # env default (not a schema-validated parameter).
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bogus")
    async with Client(mcp) as client:
        result = await client.call_tool("claude_ask", {"prompt": "x"}, raise_on_error=False)
    # F3: error envelope rides on a native is_error result, not a "success".
    assert result.is_error is True
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "unsupported_config_mode"


async def test_bogus_env_access_is_structured_error(fake_claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_ACCESS", "bogus")
    async with Client(mcp) as client:
        result = await client.call_tool("claude_ask", {"prompt": "x"}, raise_on_error=False)
    data = structured(result)
    assert data["error"]["code"] == "unsupported_access"


async def test_bare_without_api_key_errors(fake_claude, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_ask", {"prompt": "x", "config_mode": "bare"}, raise_on_error=False
        )
    data = structured(result)
    assert data["error"]["code"] == "api_key_missing"


async def test_success_response_carries_request_id(fake_claude):
    # F7: successful responses also carry a correlation id in meta.
    async with Client(mcp) as client:
        result = await client.call_tool("claude_ask", {"prompt": "is this safe?"})
    assert structured(result)["meta"]["request_id"]


async def test_status_reports_resolved_defaults(monkeypatch):
    # F5: agents can see the env-driven defaults a no-arg paid call would use.
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "scoped")
    monkeypatch.setenv("CLAUDE_IN_CODEX_MAX_BUDGET_USD", "99")  # above clamp
    async with Client(mcp) as client:
        result = await client.call_tool("claude_status", {})
    rd = structured(result)["resolved_defaults"]
    assert rd["config_mode"] == "scoped"
    assert rd["access"] == "toolless"
    assert rd["effort"] == "xhigh"  # depth-first default effort
    assert rd["max_budget_usd"] == 5.0  # clamped to MAX_BUDGET_USD
    assert rd["timeout_seconds"] == 180
    assert rd["budget_bounds"] == [0.01, 5.0]
    assert rd["timeout_bounds"] == [10, 600]


async def test_status_reports_readiness(monkeypatch):
    # claude_status must surface auth + version-compatibility for FREE, so an
    # agent can detect a logged-out or incompatible CLI before any paid call.
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")

    class _Ver:
        stdout = "2.1.162 (Claude Code)"

    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: _Ver())
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        result = await client.call_tool("claude_status", {})
    data = structured(result)
    assert data["claude_authenticated"] is True
    assert data["version_supported"] is True
    assert data["ready"] is True
    assert data["readiness_detail"].startswith("ready:")
    assert "version_warning" not in data  # supported version -> no warning
    assert "flags_warning" not in data  # probe lists every expected flag


async def test_status_ready_despite_untested_major(monkeypatch):
    # A claude major outside the tested range is advisory: ready stays True (so an
    # agent does not self-block) but version_warning explains the mismatch.
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")

    class _Ver:
        stdout = "3.0.0 (Claude Code)"

    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: _Ver())
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        result = await client.call_tool("claude_status", {})
    data = structured(result)
    assert data["version_supported"] is False
    assert data["ready"] is True  # version no longer gates readiness
    assert "version_warning" in data and "3.0.0" in data["version_warning"]


async def test_status_not_ready_when_logged_out(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")

    class _Ver:
        stdout = "2.1.162 (Claude Code)"

    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: _Ver())
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (False, "Not logged in"))
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        result = await client.call_tool("claude_status", {})
    data = structured(result)
    assert data["claude_authenticated"] is False
    assert data["ready"] is False
    assert "no authenticated session" in data["readiness_detail"]


async def test_env_default_config_mode_used(fake_claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "scoped")
    async with Client(mcp) as client:
        result = await client.call_tool("claude_ask", {"prompt": "x"})
    data = structured(result)
    assert data["meta"]["config_mode"] == "scoped"  # env default applied (param was None)


async def test_review_changes_validates_before_context(fake_claude, monkeypatch, tmp_path):
    # A bad env config_mode must error even though cwd is not a git repo —
    # proving option validation happens before git is touched.
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bogus")
    monkeypatch.chdir(tmp_path)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_review_changes", {"scope": "working_tree"}, raise_on_error=False
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "unsupported_config_mode"


async def test_review_changes_runs_in_git_repo(fake_claude, monkeypatch, git_repo):
    monkeypatch.chdir(git_repo)
    async with Client(mcp) as client:
        result = await client.call_tool("claude_review_changes", {"scope": "working_tree"})
    data = structured(result)
    assert data["ok"] is True
    assert data["verdict"] == "concerns"


async def test_review_changes_filters_paths_and_echoes_meta(fake_claude, git_repo):
    import subprocess as _sp

    (git_repo / "other.py").write_text("value = 1\n")
    _sp.run(["git", "add", "-Nf", "other.py"], cwd=git_repo, check=True)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_review_changes",
            {
                "scope": "working_tree",
                "paths": ["other.py"],
                "workspace_root": str(git_repo),
            },
        )
    data = structured(result)
    assert data["ok"] is True
    assert data["meta"]["paths"] == ["other.py"]


async def test_review_changes_empty_diff_skips_paid_call(monkeypatch, git_repo):
    import subprocess as _sp

    import claude_in_codex.server as srv

    _sp.run(["git", "checkout", "--", "app.py"], cwd=git_repo, check=True)

    async def fail_run(*args, **kwargs):
        raise AssertionError("paid call should not run")

    monkeypatch.setattr(srv, "run_claude_async", fail_run)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_review_changes", {"scope": "working_tree", "workspace_root": str(git_repo)}
        )
    data = structured(result)
    assert data["ok"] is True
    assert data["verdict"] == "pass"
    assert "No changes" in data["summary"]
    assert data["context_summary"]["files_changed"] == 0


async def test_review_changes_empty_filtered_diff_is_transparent(monkeypatch, git_repo):
    import claude_in_codex.server as srv

    async def fail_run(*args, **kwargs):
        raise AssertionError("paid call should not run")

    monkeypatch.setattr(srv, "run_claude_async", fail_run)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_review_changes",
            {"scope": "working_tree", "paths": ["missing.py"], "workspace_root": str(git_repo)},
        )
    data = structured(result)
    assert data["ok"] is True
    assert data["meta"]["paths"] == ["missing.py"]
    assert "matched paths" in data["summary"]
    assert data["context_summary"]["files_changed"] == 0


async def test_invalid_paths_are_structured_error(fake_claude, git_repo):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_review_changes",
            {"scope": "working_tree", "paths": ["../secret"], "workspace_root": str(git_repo)},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_paths"
    assert data["error"]["offending_param"] == "paths"
    assert "repo-relative" in data["error"]["repair"]


async def test_adversarial_empty_attached_diff_skips_paid_call(monkeypatch, git_repo):
    import subprocess as _sp

    import claude_in_codex.server as srv

    _sp.run(["git", "checkout", "--", "app.py"], cwd=git_repo, check=True)

    async def fail_run(*args, **kwargs):
        raise AssertionError("paid call should not run")

    monkeypatch.setattr(srv, "run_claude_async", fail_run)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_adversarial_review",
            {"target": "review plan", "scope": "working_tree", "workspace_root": str(git_repo)},
        )
    data = structured(result)
    assert data["ok"] is True
    assert data["verdict"] == "unknown"
    assert data["context_summary"]["files_changed"] == 0


async def test_adversarial_without_scope_still_calls_claude(fake_claude, tmp_path):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_adversarial_review", {"target": "review plan", "workspace_root": str(tmp_path)}
        )
    assert structured(result)["ok"] is True


async def test_adversarial_paths_without_scope_is_invalid(fake_claude, tmp_path):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_adversarial_review",
            {"target": "review plan", "paths": ["src"], "workspace_root": str(tmp_path)},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_paths"
    assert data["error"]["offending_param"] == "paths"


async def test_adversarial_invalid_scope_param_rejected_by_schema(
    fake_claude, monkeypatch, git_repo
):
    # F2: an invalid scope value is rejected by the enum schema before execution.
    monkeypatch.chdir(git_repo)
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc:
            await client.call_tool(
                "claude_adversarial_review", {"target": "skip locking", "scope": "bogus"}
            )
    assert "working_tree" in str(exc.value)


async def test_paid_tool_descriptions_do_not_inline_error_catalogs(fake_claude):
    tools = await _tools_by_name()
    for name in PAID_TOOLS:
        desc = tools[name].description.lower()
        assert "possible error codes" not in desc, name
        assert "validation error" not in desc, name


async def test_adversarial_bad_base_ref_is_structured_error(fake_claude, monkeypatch, git_repo):
    # A malformed base ref must report invalid_base (not invalid_scope) so the
    # agent repairs the right parameter.
    monkeypatch.chdir(git_repo)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_adversarial_review",
            {"target": "skip locking", "scope": "branch", "base": "-badref"},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_base"
    assert data["error"]["offending_param"] == "base"


async def test_paid_tools_declare_cost_safety_hints():
    # F4: paid, non-idempotent calls expose machine-readable hints, not just prose.
    tools = await _tools_by_name()
    for name in PAID_TOOLS:
        ann = tools[name].annotations
        assert ann is not None, name
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is False, name
        assert ann.idempotentHint is False, name


async def test_job_tools_declare_state_hints():
    tools = await _tools_by_name()
    assert tools["claude_review_changes_async"].annotations.readOnlyHint is False
    assert tools["claude_review_changes_async"].annotations.idempotentHint is False
    assert tools["claude_job_status"].annotations.readOnlyHint is False
    assert tools["claude_job_status"].annotations.idempotentHint is False
    assert tools["claude_job_result"].annotations.readOnlyHint is False
    assert tools["claude_job_result"].annotations.idempotentHint is False
    assert tools["claude_job_consume_result"].annotations.readOnlyHint is False
    assert tools["claude_job_consume_result"].annotations.idempotentHint is False
    assert tools["claude_job_cancel"].annotations.readOnlyHint is False
    assert tools["claude_job_cancel"].annotations.idempotentHint is False


async def test_review_uses_workspace_root_over_cwd(fake_claude, monkeypatch, git_repo, tmp_path):
    # F1: with cwd pointed at an unrelated (non-repo) dir, an explicit
    # workspace_root makes the review target the intended repo.
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_review_changes", {"scope": "working_tree", "workspace_root": str(git_repo)}
        )
    data = structured(result)
    assert data["ok"] is True
    assert data["meta"]["cwd"] == str(git_repo)
    assert data["meta"]["workspace_source"] == "param"


async def test_review_invalid_workspace_root_is_structured_error(fake_claude):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_review_changes",
            {"scope": "working_tree", "workspace_root": "/no/such/dir/xyz"},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_workspace_root"
    assert data["error"]["offending_param"] == "workspace_root"


async def test_review_invalid_root_without_param_does_not_blame_workspace_root(
    fake_claude, tmp_path
):
    missing = tmp_path / "missing"
    async with Client(mcp, roots=[missing.as_uri()]) as client:
        result = await client.call_tool(
            "claude_review_changes", {"scope": "working_tree"}, raise_on_error=False
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_workspace_root"
    assert "offending_param" not in data["error"]
    assert "workspace_root 'None'" not in data["error"]["message"]


async def test_review_workspace_outside_roots_is_structured_error(fake_claude, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    async with Client(mcp, roots=[root.as_uri()]) as client:
        result = await client.call_tool(
            "claude_review_changes",
            {"scope": "working_tree", "workspace_root": str(outside)},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "workspace_outside_roots"
    assert data["error"]["offending_param"] == "workspace_root"


async def test_review_changes_async_lifecycle(monkeypatch, git_repo, tmp_path):
    # End-to-end through the MCP surface: launch async -> poll status -> get the
    # same envelope as the sync tool. build_command is replaced with a fake that
    # writes a known claude envelope, so no real CLI runs.
    import json as _json

    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    inner = {
        "summary": "off-by-one",
        "verdict": "concerns",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": _json.dumps(inner),
            "total_cost_usd": 0.02,
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
    )
    monkeypatch.setattr(
        srv, "build_command", lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], [])
    )

    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
        assert started["ok"] is True
        assert started["status"] == "running"
        assert started["poll_after_ms"] == 1000
        assert started["ttl_seconds"] > 0
        job_id = started["job_id"]

        import time as _time

        deadline = _time.time() + 5
        status = "running"
        while _time.time() < deadline:
            st = structured(
                await client.call_tool(
                    "claude_job_status", {"job_id": job_id, "workspace_root": str(git_repo)}
                )
            )
            status = st["status"]
            assert st["poll_after_ms"] == 1000
            assert st["ttl_seconds"] > 0
            if status != "running":
                break
            await anyio.sleep(0.05)
        assert status == "done"

        res = structured(
            await client.call_tool(
                "claude_job_result", {"job_id": job_id, "workspace_root": str(git_repo)}
            )
        )
    assert res["ok"] is True
    assert res["verdict"] == "concerns"
    assert res["meta"]["job_id"] == job_id


async def test_review_changes_async_spawn_failure_is_structured(monkeypatch, git_repo, tmp_path):
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        srv,
        "build_command",
        lambda *a, **k: (["definitely-no-such-claude-binary-xyz"], []),
    )

    async with Client(mcp) as client:
        result = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
        jobs = structured(
            await client.call_tool("claude_job_list", {"workspace_root": str(git_repo)})
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "claude_not_found"
    assert jobs["jobs"] == []


async def test_review_changes_async_other_oserror_is_internal_error(
    monkeypatch, git_repo, tmp_path
):
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(srv, "build_command", lambda *a, **k: (["claude"], []))

    def fake_start_job(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(srv.jobs, "start_job", fake_start_job)

    async with Client(mcp) as client:
        result = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "internal_error"
    assert "Failed to start async job" in result["error"]["message"]


async def test_job_result_not_found_is_structured_error(tmp_path, monkeypatch, git_repo):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_job_result",
            {"job_id": "deadbeef", "workspace_root": str(git_repo)},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "job_not_found"


async def test_job_consume_result_deletes_finished_record(monkeypatch, git_repo, tmp_path):
    import json as _json
    import time as _time

    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    inner = {
        "summary": "ok",
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    envelope = _json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": _json.dumps(inner)}
    )
    monkeypatch.setattr(
        srv, "build_command", lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], [])
    )

    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
        job_id = started["job_id"]
        deadline = _time.time() + 5
        while _time.time() < deadline:
            st = structured(
                await client.call_tool(
                    "claude_job_status", {"job_id": job_id, "workspace_root": str(git_repo)}
                )
            )
            if st["status"] == "done":
                break
            await anyio.sleep(0.05)
        res = structured(
            await client.call_tool(
                "claude_job_consume_result", {"job_id": job_id, "workspace_root": str(git_repo)}
            )
        )
        missing = structured(
            await client.call_tool(
                "claude_job_status",
                {"job_id": job_id, "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )

    assert res["ok"] is True
    assert res["meta"]["job_id"] == job_id
    assert missing["error"]["code"] == "job_not_found"


async def test_capabilities_tool_returns_structured_contract():
    # F7: the capability/version contract is available as structured data, not
    # only as a prose resource.
    async with Client(mcp) as client:
        result = await client.call_tool("claude_capabilities", {})
    data = structured(result)
    assert data["fingerprint"] == "claude-in-codex/0.1/schema-25"
    assert data["transport"] == "stdio"
    assert set(data["paid_tools"]) == {
        "claude_ask",
        "claude_review_changes",
        "claude_adversarial_review",
        "claude_review_changes_async",
    }
    assert "claude_status" in data["free_tools"]
    for lifecycle in (
        "claude_job_status",
        "claude_job_result",
        "claude_job_consume_result",
        "claude_job_cancel",
    ):
        assert lifecycle in data["free_tools"]
    details = {item["name"]: item for item in data["tool_details"]}
    assert set(details) == set(data["paid_tools"]) | set(data["free_tools"]) - {
        "claude_capabilities",
    }
    assert details["claude_review_changes"]["cost"] == "paid"
    assert details["claude_review_changes"]["required_params"] == ["scope"]
    assert {"config_mode", "access", "model", "max_budget_usd"} <= set(
        details["claude_ask"]["key_optional_params"]
    )
    assert {"config_mode", "access", "model", "timeout_seconds"} <= set(
        details["claude_review_changes"]["key_optional_params"]
    )
    assert "paths" in details["claude_review_changes"]["key_optional_params"]
    assert "paths" in details["claude_review_changes_async"]["key_optional_params"]
    assert "paths" in details["claude_adversarial_review"]["key_optional_params"]
    assert {"config_mode", "paths"} <= set(details["claude_review_dry_run"]["key_optional_params"])
    assert details["claude_status"]["cost"] == "free"
    assert data["negative_scope"]  # non-empty list of what it won't do
    assert data["prerequisites"]
    assert "fingerprint" in data["deprecation_policy"]


async def test_capabilities_disclose_data_egress():
    # The egress disclosure must be machine-readable on the contract, not only prose.
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_capabilities", {}))
    egress = data["data_egress"]
    assert "Anthropic" in egress
    assert "redact" in egress.lower()
    # It must state coverage now spans returned output, and still name what is NOT
    # covered: the caller's free-form inputs and access=readonly direct reads.
    assert "returned" in egress.lower()
    assert "verbatim" in egress.lower()
    assert "readonly" in egress


async def test_returned_model_output_is_redacted(monkeypatch):
    # Pins the #66 behavior: best-effort secret redaction now covers Claude's
    # returned output (summary/findings/raw text), not just the diff sent TO Claude.
    # If this is ever weakened, the data_egress / docstring / SECURITY.md text must change too.
    import claude_in_codex.server as srv
    from claude_in_codex.claude import ClaudeRun

    secret = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    inner = {"summary": f"saw token {secret}", "verdict": "concerns", "confidence": "high"}
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(inner),
            "session_id": "s",
            "modelUsage": {"claude-sonnet-4-6": {}},
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )

    async def fake_run(cmd, cwd, timeout_seconds, stdin_text=None, *, config_mode=None):
        return ClaudeRun(stdout=envelope, stderr="", exit_code=0, elapsed_ms=1, timed_out=False)

    monkeypatch.setattr(srv, "run_claude_async", fake_run)
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_ask", {"prompt": "hi"}))
    assert secret not in data["summary"]  # returned output is scrubbed
    assert "[redacted: secret value]" in data["summary"]

    # The disclosure now states returned output is covered.
    egress = _capabilities_payload()["data_egress"].lower()
    assert "returned" in egress and "redact" in egress


async def test_paid_tool_docstrings_disclose_egress():
    paid = (
        "claude_ask",
        "claude_review_changes",
        "claude_adversarial_review",
        "claude_review_changes_async",
    )
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in paid:
        desc = tools[name].description or ""
        assert "Anthropic" in desc, f"{name} docstring omits Anthropic egress"
        assert "redact" in desc.lower(), f"{name} docstring omits redaction scope"


async def test_list_tools_includes_new_free_tools():
    names = set(await _tools_by_name())
    assert {"claude_review_dry_run", "claude_job_list", "claude_capabilities"} <= names


async def test_claude_capabilities_returns_expected_free_tools():
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_capabilities", {}))
    assert "claude_review_dry_run" in data["free_tools"]
    assert "claude_job_list" in data["free_tools"]
    assert "claude_models" in data["free_tools"]
    # The readonly redaction-bypass caveat is now in the negative scope.
    assert any("readonly" in s for s in data["negative_scope"])


async def test_dry_run_previews_without_spending(monkeypatch, git_repo):
    # No fake_claude: a real paid call would fail. The dry-run must not call Claude.
    monkeypatch.chdir(git_repo)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    assert data["ok"] is True
    assert data["tool"] == "claude_review_dry_run"
    assert data["cwd"] == str(git_repo)
    assert data["workspace_source"] == "param"
    assert data["diff_bytes"] > 0
    assert data["max_diff_bytes"] > 0
    assert data["truncated"] is False
    assert data["context_summary"]["files_changed"] == 1
    assert "fingerprint" in data


async def test_dry_run_echoes_paths_and_filtered_summary(monkeypatch, git_repo):
    import subprocess as _sp

    (git_repo / "other.py").write_text("value = 1\n")
    _sp.run(["git", "add", "-Nf", "other.py"], cwd=git_repo, check=True)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run",
                {
                    "scope": "working_tree",
                    "paths": ["other.py"],
                    "workspace_root": str(git_repo),
                },
            )
        )
    assert data["ok"] is True
    assert data["paths"] == ["other.py"]
    assert data["context_summary"]["files_changed"] == 1


async def test_dry_run_reports_redaction_count(monkeypatch, git_repo):
    import subprocess as _sp

    (git_repo / ".env").write_text("API_KEY=supersecret\n")
    _sp.run(["git", "add", "-Nf", ".env"], cwd=git_repo, check=True)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    assert data["redacted_paths_count"] >= 1
    assert any(".env" in p for p in data["redacted_paths"])


async def test_dry_run_reports_workspace_hooks(monkeypatch, git_repo):
    monkeypatch.delenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", raising=False)
    settings_dir = git_repo / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text('{"hooks":{"SessionStart":[]}}')
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    assert data["resolved_config_mode"] == "inherit"
    assert data["hooks_disabled"] is False
    assert data["workspace_hook_settings"] == [".claude/settings.json"]
    assert any("hooks" in warning for warning in data["security_warnings"])


async def test_dry_run_does_not_claim_hooks_disabled_when_bare_unavailable(monkeypatch, git_repo):
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bare")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    assert data["resolved_config_mode"] == "bare"
    assert data["hooks_disabled"] is False


async def test_dry_run_claims_hooks_disabled_for_safe_without_api_key(monkeypatch, git_repo):
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "safe")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    assert data["resolved_config_mode"] == "safe"
    assert data["hooks_disabled"] is True


async def test_dry_run_accepts_per_call_safe_config(monkeypatch, git_repo):
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "inherit")
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "config_mode": "safe",
                },
            )
        )
    assert data["ok"] is True
    assert data["resolved_config_mode"] == "safe"
    assert data["hooks_disabled"] is True


async def test_dry_run_rejects_safe_when_help_omits_flag(monkeypatch, git_repo):
    import claude_in_codex.server as srv

    supported = frozenset(ALWAYS_SEND_FLAGS).union(HELP_GATED_FLAGS) - frozenset({"--safe-mode"})
    monkeypatch.setattr(
        srv.preflight,
        "flag_support",
        lambda *a, **k: FlagSupport(supported=supported, help_parsed=True),
    )
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "config_mode": "safe",
                },
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "unsupported_config_mode"
    assert data["error"]["offending_param"] == "config_mode"


async def test_review_result_reports_redacted_paths(fake_claude, git_repo):
    import subprocess as _sp

    (git_repo / ".env").write_text("API_KEY=supersecret\n")
    _sp.run(["git", "add", "-Nf", ".env"], cwd=git_repo, check=True)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    assert data["ok"] is True
    assert any(".env" in p for p in data["meta"]["redacted_paths"])


async def test_paid_result_reports_workspace_hooks(fake_claude, git_repo):
    settings_dir = git_repo / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.local.json").write_text('{"hooks":{"SessionStart":[]}}')
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    assert any(
        ".claude/settings.local.json" in warning for warning in data["meta"]["security_warnings"]
    )


async def test_async_empty_diff_skips_job_start(monkeypatch, git_repo, tmp_path):
    import subprocess as _sp

    import claude_in_codex.server as srv

    _sp.run(["git", "checkout", "--", "app.py"], cwd=git_repo, check=True)
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        srv,
        "build_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("job should not start")),
    )
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
    assert data["ok"] is True
    assert data["tool"] == "claude_review_changes"
    assert data["verdict"] == "pass"


async def test_async_result_reports_redacted_paths(monkeypatch, git_repo, tmp_path):
    import json as _json
    import subprocess as _sp
    import time as _time

    import claude_in_codex.server as srv

    (git_repo / ".env").write_text("API_KEY=supersecret\n")
    _sp.run(["git", "add", "-Nf", ".env"], cwd=git_repo, check=True)
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    inner = {
        "summary": "ok",
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    envelope = _json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": _json.dumps(inner)}
    )
    monkeypatch.setattr(
        srv, "build_command", lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], [])
    )

    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "paths": [".env"], "workspace_root": str(git_repo)},
            )
        )
        assert started["meta"]["paths"] == [".env"]
        job_id = started["job_id"]
        deadline = _time.time() + 5
        while _time.time() < deadline:
            st = structured(
                await client.call_tool(
                    "claude_job_status", {"job_id": job_id, "workspace_root": str(git_repo)}
                )
            )
            if st["status"] == "done":
                break
            await anyio.sleep(0.05)
        result = structured(
            await client.call_tool(
                "claude_job_result", {"job_id": job_id, "workspace_root": str(git_repo)}
            )
        )
    assert result["meta"]["paths"] == [".env"]
    assert ".env" in result["meta"]["redacted_paths"]


async def test_dry_run_bad_base_is_structured_error(monkeypatch, git_repo):
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run",
                {
                    "scope": "branch",
                    "base": "-badref",
                    "config_mode": "safe",
                    "workspace_root": str(git_repo),
                },
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_base"
    assert data["meta"]["config_mode"] == "safe"


async def test_dry_run_nonexistent_base_is_invalid_base(git_repo):
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run",
                {
                    "scope": "branch",
                    "base": "definitely-not-a-real-branch",
                    "workspace_root": str(git_repo),
                },
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_base"
    assert data["error"]["offending_param"] == "base"


async def test_cwd_resolution_sets_workspace_warning(fake_claude, monkeypatch, git_repo):
    # When the workspace falls back to cwd (no param, no roots), the success meta
    # must carry workspace_warning so an agent can notice the footgun.
    monkeypatch.chdir(git_repo)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool("claude_review_changes", {"scope": "working_tree"})
        )
    assert data["ok"] is True
    assert data["meta"]["workspace_source"] == "cwd"
    assert "workspace_root" in data["meta"]["workspace_warning"]


async def test_param_resolution_has_no_workspace_warning(fake_claude, git_repo):
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    assert data["ok"] is True
    assert "workspace_warning" not in data["meta"]  # None is dropped by exclude_none


async def test_meta_echoes_requested_budget(fake_claude, monkeypatch, git_repo):
    monkeypatch.chdir(git_repo)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool("claude_ask", {"prompt": "x", "max_budget_usd": 0.25})
        )
    assert data["meta"]["requested_max_budget_usd"] == 0.25


async def test_paid_prompt_is_passed_over_stdin_not_argv(monkeypatch, tmp_path):
    import claude_in_codex.server as srv
    from claude_in_codex.claude import ClaudeRun

    captured = {}
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(
                {
                    "summary": "ok",
                    "verdict": "pass",
                    "confidence": "high",
                    "findings": [],
                    "questions": [],
                    "assumptions": [],
                }
            ),
        }
    )

    async def fake_run(cmd, cwd, timeout_seconds, stdin_text=None, *, config_mode=None):
        captured["cmd"] = cmd
        captured["stdin_text"] = stdin_text
        captured["config_mode"] = config_mode
        return ClaudeRun(stdout=envelope, stderr="", exit_code=0, elapsed_ms=1, timed_out=False)

    monkeypatch.setattr(srv, "run_claude_async", fake_run)
    prompt = "sensitive prompt --model should-not-be-argv"
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_ask", {"prompt": prompt, "workspace_root": str(tmp_path)}
            )
        )
    assert data["ok"] is True
    assert all(prompt not in arg for arg in captured["cmd"])
    assert prompt in captured["stdin_text"]
    assert captured["config_mode"] == "inherit"


async def test_status_auth_detail_is_redacted(monkeypatch):
    # claude_status must not leak the account email/org from `claude auth status`.
    import claude_in_codex.claude as cl

    class _Proc:
        returncode = 0
        stdout = "Logged in as alice@example.com (org: Acme Corp)"
        stderr = ""

    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: _Proc())
    logged_in, detail = cl.auth_status(config_mode="inherit")
    assert logged_in is True
    assert detail and "alice@example.com" not in detail
    assert "Acme Corp" not in detail


async def test_job_list_recovers_job_ids(monkeypatch, git_repo, tmp_path):
    import json as _json
    import time as _time

    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    inner = {
        "summary": "ok",
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": _json.dumps(inner),
            "total_cost_usd": 0.02,
        }
    )
    monkeypatch.setattr(
        srv, "build_command", lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], [])
    )

    async with Client(mcp) as client:
        empty = structured(
            await client.call_tool("claude_job_list", {"workspace_root": str(git_repo)})
        )
        assert empty["jobs"] == []

        started = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
        job_id = started["job_id"]
        deadline = _time.time() + 5
        while _time.time() < deadline:
            st = structured(
                await client.call_tool(
                    "claude_job_status", {"job_id": job_id, "workspace_root": str(git_repo)}
                )
            )
            if st["status"] == "done":
                break
            await anyio.sleep(0.05)

        listing = structured(
            await client.call_tool("claude_job_list", {"workspace_root": str(git_repo)})
        )
    assert listing["ok"] is True
    ids = [j["job_id"] for j in listing["jobs"]]
    assert job_id in ids
    job = next(j for j in listing["jobs"] if j["job_id"] == job_id)
    assert job["status"] == "done"
    assert job["result_available"] is True


async def test_paid_failure_reports_cost_on_error_meta(monkeypatch):
    # A non-zero claude exit that still emitted a cost-bearing JSON envelope
    # (e.g. budget_exceeded) must report cost_usd/usage on the error meta, just
    # like the is_error-envelope path does.
    import claude_in_codex.server as srv
    from claude_in_codex.claude import ClaudeRun

    envelope = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "subtype": "error_max_budget_usd",
            "result": "over budget",
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }
    )

    async def fake_run(cmd, cwd, timeout_seconds, stdin_text=None, *, config_mode=None):
        return ClaudeRun(stdout=envelope, stderr="", exit_code=1, elapsed_ms=5, timed_out=False)

    monkeypatch.setattr(srv, "run_claude_async", fake_run)
    async with Client(mcp) as client:
        result = await client.call_tool("claude_ask", {"prompt": "x"}, raise_on_error=False)
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "budget_exceeded"
    assert "$0.10-$0.20" in data["error"]["repair"]
    assert data["meta"]["cost_usd"] == 0.05
    assert data["meta"]["usage"]["input_tokens"] == 10


@pytest.mark.parametrize(
    "tool,args",
    [
        ("claude_ask", {"prompt": "x"}),
        ("claude_adversarial_review", {"target": "x"}),
        ("claude_review_changes_async", {"scope": "working_tree"}),
        ("claude_job_status", {"job_id": "j"}),
        ("claude_job_result", {"job_id": "j"}),
        ("claude_job_consume_result", {"job_id": "j"}),
        ("claude_job_cancel", {"job_id": "j"}),
        ("claude_review_dry_run", {"scope": "working_tree"}),
        ("claude_job_list", {}),
    ],
)
async def test_workspace_error_branch_for_each_tool(tool, args):
    async with Client(mcp) as client:
        result = await client.call_tool(
            tool, {**args, "workspace_root": "/no/such/dir/xyz"}, raise_on_error=False
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_workspace_root"


async def test_job_consume_and_cancel_not_found(tmp_path, monkeypatch, git_repo):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    async with Client(mcp) as client:
        consume = structured(
            await client.call_tool(
                "claude_job_consume_result",
                {"job_id": "nope", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
        cancel = structured(
            await client.call_tool(
                "claude_job_cancel",
                {"job_id": "nope", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
    assert consume["error"]["code"] == "job_not_found"
    assert cancel["error"]["code"] == "job_not_found"


async def test_adversarial_and_async_resolve_error(fake_claude, monkeypatch, git_repo, tmp_path):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bogus")
    async with Client(mcp) as client:
        adv = structured(
            await client.call_tool(
                "claude_adversarial_review",
                {"target": "x", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
        asy = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
    assert adv["error"]["code"] == "unsupported_config_mode"
    assert asy["error"]["code"] == "unsupported_config_mode"


def _fake_ctx(**over):
    base = dict(
        truncated=False,
        truncation_hint=None,
        text="diff",
        diff_bytes=4,
        redacted_paths=[],
        summary=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("claude_review_changes", {"scope": "working_tree"}),
        ("claude_adversarial_review", {"target": "x", "scope": "working_tree"}),
        ("claude_review_changes_async", {"scope": "working_tree"}),
        ("claude_review_dry_run", {"scope": "working_tree"}),
    ],
)
async def test_invalid_scope_from_gather_context(tool, args, monkeypatch, git_repo, tmp_path):
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        srv, "gather_context", lambda *a, **k: (_ for _ in ()).throw(srv.InvalidScopeError("bad"))
    )
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                tool, {**args, "workspace_root": str(git_repo)}, raise_on_error=False
            )
        )
    assert data["error"]["code"] == "invalid_scope"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("claude_review_changes", {"scope": "working_tree"}),
        ("claude_adversarial_review", {"target": "x", "scope": "working_tree"}),
        ("claude_review_changes_async", {"scope": "working_tree"}),
        ("claude_review_dry_run", {"scope": "working_tree"}),
    ],
)
async def test_internal_error_from_gather_context(tool, args, monkeypatch, git_repo, tmp_path):
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        srv, "gather_context", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git exploded"))
    )
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                tool, {**args, "workspace_root": str(git_repo)}, raise_on_error=False
            )
        )
    assert data["error"]["code"] == "internal_error"


@pytest.mark.parametrize(
    "exc_type,code,repair",
    [
        (
            "NotAGitRepoError",
            "not_a_git_repo",
            "Run reviews from inside a git repository, or pass workspace_root pointing at one.",
        ),
        ("GitUnavailableError", "git_unavailable", "Install git and ensure it is on PATH."),
    ],
)
@pytest.mark.parametrize(
    "tool,args",
    [
        ("claude_review_changes", {"scope": "working_tree"}),
        ("claude_adversarial_review", {"target": "x", "scope": "working_tree"}),
        ("claude_review_changes_async", {"scope": "working_tree"}),
        ("claude_review_dry_run", {"scope": "working_tree"}),
    ],
)
async def test_git_environment_errors_from_gather_context(
    tool, args, exc_type, code, repair, monkeypatch, git_repo, tmp_path
):
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    exc = getattr(srv, exc_type)("boom")
    monkeypatch.setattr(srv, "gather_context", lambda *a, **k: (_ for _ in ()).throw(exc))
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                tool, {**args, "workspace_root": str(git_repo)}, raise_on_error=False
            )
        )
    assert data["error"]["code"] == code
    assert data["error"]["repair"] == repair
    assert data["error"]["retryable"] is False


@pytest.mark.parametrize(
    "tool,args",
    [
        ("claude_review_changes", {"scope": "working_tree"}),
        ("claude_adversarial_review", {"target": "x", "scope": "working_tree"}),
        ("claude_review_changes_async", {"scope": "working_tree"}),
    ],
)
async def test_truncated_diff_is_context_too_large(tool, args, monkeypatch, git_repo, tmp_path):
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        srv, "gather_context", lambda *a, **k: _fake_ctx(truncated=True, truncation_hint="too big")
    )
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                tool, {**args, "workspace_root": str(git_repo)}, raise_on_error=False
            )
        )
    assert data["error"]["code"] == "context_too_large"
    assert data["meta"]["truncated"] is True


@pytest.mark.parametrize("tool", ["claude_review_changes", "claude_review_changes_async"])
async def test_bad_base_ref_is_invalid_base(tool, fake_claude, monkeypatch, git_repo, tmp_path):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                tool,
                {"scope": "branch", "base": "-badref", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
    assert data["error"]["code"] == "invalid_base"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("claude_review_changes", {"scope": "branch"}),
        ("claude_review_changes_async", {"scope": "branch"}),
        ("claude_adversarial_review", {"target": "review", "scope": "branch"}),
    ],
)
async def test_nonexistent_base_ref_is_invalid_base(
    tool, args, fake_claude, monkeypatch, git_repo, tmp_path
):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                tool,
                {
                    **args,
                    "base": "definitely-not-a-real-branch",
                    "workspace_root": str(git_repo),
                },
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_base"
    assert data["error"]["offending_param"] == "base"


async def test_adversarial_with_nonempty_diff_calls_claude(fake_claude, git_repo):
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_adversarial_review",
                {"target": "review", "scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
    assert data["ok"] is True
    assert data["verdict"] == "concerns"


async def test_execute_nonzero_exit_non_json_stdout(monkeypatch, tmp_path):
    import claude_in_codex.server as srv
    from claude_in_codex.claude import ClaudeRun

    async def fake_run(cmd, cwd, timeout_seconds, stdin_text=None, *, config_mode=None):
        return ClaudeRun(
            stdout="not json at all", stderr="boom", exit_code=1, elapsed_ms=5, timed_out=False
        )

    monkeypatch.setattr(srv, "run_claude_async", fake_run)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_ask", {"prompt": "x", "workspace_root": str(tmp_path)}, raise_on_error=False
        )
    assert structured(result)["ok"] is False


async def test_file_roots_none_ctx_returns_empty():
    from claude_in_codex.server import _file_roots

    assert await _file_roots(None) == []


def test_contained_by_value_error(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(
        srv.os.path,
        "commonpath",
        lambda _paths: (_ for _ in ()).throw(ValueError("different drives")),
    )
    assert srv._contained_by("/a", "/b") is False


async def test_status_version_probe_exception_keeps_version_none(monkeypatch):
    import claude_in_codex.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        srv.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("cannot exec"))
    )
    monkeypatch.setattr(srv, "auth_status", lambda *a, **k: (True, "Logged in"))
    _patch_full_flag_support(monkeypatch)
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_status", {}))
    assert data["claude_found"] is True
    assert "claude_version" not in data  # None dropped by exclude_none


async def test_capabilities_resource_returns_summary():
    async with Client(mcp) as client:
        contents = await client.read_resource("claude-in-codex://capabilities")
    assert "claude-in-codex" in contents[0].text


def test_main_runs_stdio(monkeypatch):
    import claude_in_codex.server as srv

    called = {}
    monkeypatch.setattr(srv.mcp, "run", lambda **k: called.update(k))
    srv.main()
    assert called == {"transport": "stdio"}


async def test_job_cancel_success_via_mcp(monkeypatch, git_repo, tmp_path):
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(srv, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
        job_id = started["job_id"]
        cancelled = structured(
            await client.call_tool(
                "claude_job_cancel", {"job_id": job_id, "workspace_root": str(git_repo)}
            )
        )
    assert cancelled["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Issue #35: explicit branch diff head
# ---------------------------------------------------------------------------


def _make_branch_with_head(git_repo):
    """Return (base, head_branch) where head_branch has one extra commit over base,
    with the repo checked back out at base so base...head reflects the head commit."""
    import subprocess as _sp

    base = _sp.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _sp.run(["git", "checkout", "--", "app.py"], cwd=git_repo, check=True)
    _sp.run(["git", "switch", "-c", "feature"], cwd=git_repo, check=True)
    (git_repo / "feature.py").write_text("value = 1\n")
    _sp.run(["git", "add", "feature.py"], cwd=git_repo, check=True)
    _sp.run(["git", "commit", "-q", "-m", "feature change"], cwd=git_repo, check=True)
    _sp.run(["git", "switch", base], cwd=git_repo, check=True)
    return base, "feature"


async def test_tool_schemas_expose_head():
    tools = await _tools_by_name()
    for name in (
        "claude_review_changes",
        "claude_review_changes_async",
        "claude_adversarial_review",
        "claude_review_dry_run",
    ):
        props = tools[name].inputSchema["properties"]
        assert "head" in props, name
        assert props["head"]["description"], name


async def test_capabilities_include_head():
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_capabilities", {}))
    details = {d["name"]: d for d in data["tool_details"]}
    for name in (
        "claude_review_changes",
        "claude_review_changes_async",
        "claude_adversarial_review",
        "claude_review_dry_run",
    ):
        assert "head" in details[name]["key_optional_params"], name


async def test_review_changes_threads_head_into_gather_prompt_and_meta(
    fake_claude, monkeypatch, git_repo
):
    import claude_in_codex.server as srv

    captured = {}
    real_build_prompt = srv.build_prompt

    def spy_build_prompt(tool, payload, context_text):
        captured["payload"] = payload
        captured["context_text"] = context_text
        return real_build_prompt(tool, payload, context_text)

    base, head = _make_branch_with_head(git_repo)
    monkeypatch.setattr(srv, "build_prompt", spy_build_prompt)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes",
                {
                    "scope": "branch",
                    "base": base,
                    "head": head,
                    "workspace_root": str(git_repo),
                },
            )
        )
    assert data["ok"] is True
    assert data["meta"]["head"] == head
    assert data["meta"]["diff_range"] == f"{base}...{head}"
    assert "feature.py" in captured["context_text"]
    assert captured["payload"]["head"] == head


async def test_review_changes_default_head_reports_effective_head(fake_claude, git_repo):
    import subprocess as _sp

    base = _sp.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes",
                {"scope": "branch", "base": base, "workspace_root": str(git_repo)},
            )
        )
    assert data["ok"] is True
    assert data["meta"]["head"] == "HEAD"
    assert data["meta"]["diff_range"] == f"{base}...HEAD"


async def test_review_changes_non_branch_leaves_head_and_range_unset(fake_claude, git_repo):
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
    assert data["ok"] is True
    assert data["meta"].get("head") is None
    assert data["meta"].get("diff_range") is None


async def test_review_changes_malformed_head_is_invalid_head(fake_claude, git_repo):
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes",
                {
                    "scope": "branch",
                    "base": "main",
                    "head": "--output=/tmp/pwn",
                    "workspace_root": str(git_repo),
                },
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_head"
    assert data["error"]["offending_param"] == "head"


async def test_review_changes_empty_head_is_invalid_head(fake_claude, git_repo):
    # An explicit empty string must surface as invalid_head, not be coalesced to HEAD.
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes",
                {
                    "scope": "branch",
                    "base": "main",
                    "head": "",
                    "workspace_root": str(git_repo),
                },
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_head"
    assert data["error"]["offending_param"] == "head"


async def test_review_changes_nonexistent_head_is_invalid_head(fake_claude, git_repo):
    import subprocess as _sp

    base = _sp.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes",
                {
                    "scope": "branch",
                    "base": base,
                    "head": "no-such-ref",
                    "workspace_root": str(git_repo),
                },
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_head"
    assert data["error"]["offending_param"] == "head"


async def test_review_changes_head_rejected_for_non_branch_scope(fake_claude, git_repo):
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_changes",
                {"scope": "working_tree", "head": "feature", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_head"
    assert data["error"]["offending_param"] == "head"


async def test_adversarial_threads_head_when_diff_attached(fake_claude, git_repo):
    base, head = _make_branch_with_head(git_repo)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_adversarial_review",
                {
                    "target": "the plan",
                    "scope": "branch",
                    "base": base,
                    "head": head,
                    "workspace_root": str(git_repo),
                },
            )
        )
    assert data["ok"] is True
    assert data["meta"]["head"] == head
    assert data["meta"]["diff_range"] == f"{base}...{head}"


async def test_adversarial_head_without_scope_is_rejected(fake_claude, git_repo):
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_adversarial_review",
                {"target": "the plan", "head": "feature", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_head"
    assert data["error"]["offending_param"] == "head"


async def test_dry_run_reports_effective_head_and_range(monkeypatch, git_repo):
    base, head = _make_branch_with_head(git_repo)
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run",
                {
                    "scope": "branch",
                    "base": base,
                    "head": head,
                    "workspace_root": str(git_repo),
                },
            )
        )
    assert data["ok"] is True
    assert data["head"] == head
    assert data["diff_range"] == f"{base}...{head}"


async def test_dry_run_non_branch_leaves_head_and_range_unset(monkeypatch, git_repo):
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_review_dry_run",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
    assert data["ok"] is True
    assert data.get("head") is None
    assert data.get("diff_range") is None


async def test_async_threads_head_into_meta_and_job(monkeypatch, git_repo, tmp_path):
    import json as _json

    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    base, head = _make_branch_with_head(git_repo)
    inner = {
        "summary": "ok",
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": _json.dumps(inner),
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
    )
    monkeypatch.setattr(
        srv, "build_command", lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], [])
    )
    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "branch",
                    "base": base,
                    "head": head,
                    "workspace_root": str(git_repo),
                },
            )
        )
        assert started["ok"] is True
        assert started["meta"]["head"] == head
        assert started["meta"]["diff_range"] == f"{base}...{head}"
        job_id = started["job_id"]

        import time as _time

        deadline = _time.time() + 5
        status = "running"
        while _time.time() < deadline:
            st = structured(
                await client.call_tool(
                    "claude_job_status", {"job_id": job_id, "workspace_root": str(git_repo)}
                )
            )
            status = st["status"]
            if status != "running":
                break
            await anyio.sleep(0.05)
        assert status == "done"
        res = structured(
            await client.call_tool(
                "claude_job_result", {"job_id": job_id, "workspace_root": str(git_repo)}
            )
        )
    assert res["ok"] is True
    assert res["meta"]["head"] == head
    assert res["meta"]["diff_range"] == f"{base}...{head}"
