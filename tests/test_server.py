import ast
import inspect
import json
import time
import types
from typing import Literal, get_args

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from pydantic import ValidationError as PydanticValidationError
from pydantic import create_model
from tests.conftest import structured

from claude_in_codex import __version__
from claude_in_codex import claude as claude_mod
from claude_in_codex import jobs as jobs_mod
from claude_in_codex.cli_contract import ALWAYS_SEND_FLAGS, HELP_GATED_FLAGS
from claude_in_codex.preflight import FlagSupport
from claude_in_codex.schemas import OUTPUT_BOUNDS, TRUNCATION_MARKER, ErrorCode, JobState
from claude_in_codex.server import (
    _ERROR_CATALOG,
    _TOOL_ERROR_CODES,
    CAPABILITY_SUMMARY,
    ValidationEnvelopeMiddleware,
    _capabilities_payload,
    _first_root,
    _resolve_workspace,
    mcp,
)


def _statically_reachable_error_codes() -> dict[str, set[str]]:
    """Per-tool error codes reachable through server.py's own call graph.

    Walks the module AST for literal first arguments to _err(...) and code= on
    ErrorInfo(...), then propagates them up through same-module calls. Codes
    passed as a variable (only _workspace_error does this) and codes raised in
    other modules are invisible here — see the caller's docstring."""
    import claude_in_codex.server as srv

    tree = ast.parse(inspect.getsource(srv))
    funcs = {
        n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    def direct(node):
        codes, calls = set(), set()
        for n in ast.walk(node):
            if not isinstance(n, ast.Call):
                continue
            name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if name == "_err" and n.args and isinstance(n.args[0], ast.Constant):
                codes.add(n.args[0].value)
            if name == "ErrorInfo":
                for kw in n.keywords:
                    if kw.arg == "code" and isinstance(kw.value, ast.Constant):
                        codes.add(kw.value.value)
            if name:
                calls.add(name)
        return codes, calls

    def resolve(name, seen=()):
        if name in seen or name not in funcs:
            return set()
        codes, calls = direct(funcs[name])
        for call in calls - {name}:
            codes |= resolve(call, (*seen, name))
        return codes

    return {tool: resolve(tool) for tool in _TOOL_ERROR_CODES}


PAID_TOOLS = (
    "claude_ask",
    "claude_review_changes",
    "claude_adversarial_review",
    "claude_review_changes_async",
    "claude_consult_async",
    "claude_adversarial_review_async",
)


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
    path, err, source, _roots = await _resolve_workspace(str(child), ctx)
    assert err is None
    assert path == str(child)
    assert source == "param"


async def test_resolve_workspace_uses_roots_when_no_param(tmp_path):
    ctx = _FakeRoots([tmp_path.as_uri()])
    path, err, source, _roots = await _resolve_workspace(None, ctx)
    assert err is None
    assert path == str(tmp_path)
    assert source == "roots"


async def test_resolve_workspace_param_must_be_inside_roots(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = _FakeRoots([root.as_uri()])
    path, err, source, _roots = await _resolve_workspace(str(outside), ctx)
    assert path is None
    assert err == "workspace_outside_roots"
    assert source is None


async def test_resolve_workspace_param_inside_roots_allowed(tmp_path):
    root = tmp_path / "root"
    child = root / "repo"
    child.mkdir(parents=True)
    ctx = _FakeRoots([root.as_uri()])
    path, err, source, _roots = await _resolve_workspace(str(child), ctx)
    assert err is None
    assert path == str(child)
    assert source == "param"


async def test_resolve_workspace_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path, err, source, _roots = await _resolve_workspace(None, _FakeRoots(raises=True))
    assert err is None
    assert path == str(tmp_path)
    assert source == "cwd"


async def test_resolve_workspace_rejects_nonexistent_param():
    path, err, source, _roots = await _resolve_workspace("/no/such/dir/xyz", _FakeRoots())
    assert path is None
    assert err == "invalid_workspace_root"


async def test_resolve_workspace_rejects_relative_param(tmp_path, monkeypatch):
    # A relative workspace_root must be rejected — it would resolve against the
    # untrusted cwd that workspace resolution exists to bypass.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    path, err, source, _roots = await _resolve_workspace("sub", _FakeRoots())
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
    assert "workspace hooks may run shell" in summary
    for mode in ("inherit", "scoped", "safe", "bare"):
        assert f"config_mode={mode}" in summary
    # Ceiling exists to keep first-read instructions compact; raised to 1100 to
    # accommodate the error-carrier disclosure (isError/ok:false envelope).
    assert len(CAPABILITY_SUMMARY) < 1100


async def test_tool_descriptions_are_concise_and_disambiguating():
    tools = await _tools_by_name()
    for tool in tools.values():
        assert len(tool.description or "") <= 450, tool.name
    assert "question or design choice" in tools["claude_consult"].description
    assert tools["claude_ask"].description.startswith("[DEPRECATED alias of claude_consult")
    assert tools["claude_review_dry_run"].description.startswith(
        "[DEPRECATED alias of claude_dry_run"
    )
    assert "git diff" in tools["claude_review_changes"].description
    assert "background" in tools["claude_review_changes_async"].description
    assert "without deleting" in tools["claude_job_result"].description
    assert "delete the stored job record" in tools["claude_job_consume_result"].description


async def test_paid_tool_descriptions_disclose_hook_boundary():
    tools = await _tools_by_name()
    for name in PAID_TOOLS:
        description = " ".join((tools[name].description or "").lower().split())
        assert "grants no bash/write tools" in description, name
        assert "workspace hooks may run shell" in description, name
        for mode in ("inherit", "scoped", "safe", "bare"):
            assert f"config_mode={mode}" in description, name


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


async def test_paid_tools_publish_budget_bounds():
    tools = await _tools_by_name()
    for name in PAID_TOOLS:
        prop = tools[name].inputSchema["properties"]["max_budget_usd"]
        number_schema = next(
            branch for branch in prop.get("anyOf", [prop]) if branch.get("type") == "number"
        )
        assert number_schema["minimum"] == 0.01, name
        assert number_schema["maximum"] == 5.0, name


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
    assert data["meta"]["fingerprint"] == "claude-in-codex/0.1/schema-37"


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
    assert data["error"]["details"]["field"] == "prompt"


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
    assert data["error"]["details"]["field"] == "evidence"


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
    data = structured(result)
    assert data["raw_defaults"]["max_budget_usd"] == 99.0
    rd = data["resolved_defaults"]
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
            "claude_review_changes",
            {"scope": "working_tree", "workspace_root": str(git_repo), "detail": "full"},
        )
        # The unspent path honors `detail` like a real result: context_summary is
        # a full-only field, so summary mode must not leak it (#94).
        at_summary = structured(
            await client.call_tool(
                "claude_review_changes", {"scope": "working_tree", "workspace_root": str(git_repo)}
            )
        )
    data = structured(result)
    assert data["ok"] is True
    assert data["verdict"] == "pass"
    assert "No changes" in data["summary"]
    assert data["context_summary"]["files_changed"] == 0
    assert "context_summary" not in at_summary
    assert at_summary["summary"] == data["summary"]


async def test_review_changes_empty_filtered_diff_is_transparent(monkeypatch, git_repo):
    import claude_in_codex.server as srv

    async def fail_run(*args, **kwargs):
        raise AssertionError("paid call should not run")

    monkeypatch.setattr(srv, "run_claude_async", fail_run)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "claude_review_changes",
            {
                "scope": "working_tree",
                "paths": ["missing.py"],
                "workspace_root": str(git_repo),
                "detail": "full",
            },
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
    assert data["error"]["details"]["field"] == "paths"
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
            {
                "target": "review plan",
                "scope": "working_tree",
                "workspace_root": str(git_repo),
                "detail": "full",
            },
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
    assert data["error"]["details"]["field"] == "paths"


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
    assert data["error"]["details"]["field"] == "base"


async def test_paid_tools_declare_cost_safety_hints():
    # Paid tools spend money and send context to Anthropic, so they are not
    # read-only. Their static annotations describe the worst config-mode case:
    # inherit/scoped hooks may run destructive shell commands. Each call spends,
    # so it is non-idempotent.
    tools = await _tools_by_name()
    for name in PAID_TOOLS:
        ann = tools[name].annotations
        assert ann is not None, name
        assert ann.readOnlyHint is False, name
        assert ann.destructiveHint is True, name
        assert ann.idempotentHint is False, name


async def test_job_tools_declare_state_hints():
    tools = await _tools_by_name()
    assert tools["claude_review_changes_async"].annotations.readOnlyHint is False
    assert tools["claude_review_changes_async"].annotations.idempotentHint is False
    # Job polling performs lazy maintenance while reading (deadline kills,
    # TTL deletion), so it is not read-only, though it never alters a
    # terminal job's stored result.
    assert tools["claude_job_status"].annotations.readOnlyHint is False
    assert tools["claude_job_result"].annotations.readOnlyHint is False
    # Consume irreversibly deletes the stored record.
    assert tools["claude_job_consume_result"].annotations.readOnlyHint is False
    assert tools["claude_job_consume_result"].annotations.destructiveHint is True
    assert tools["claude_job_consume_result"].annotations.idempotentHint is False
    # Cancel is idempotent: already-terminal jobs are returned unchanged.
    assert tools["claude_job_cancel"].annotations.readOnlyHint is False
    assert tools["claude_job_cancel"].annotations.idempotentHint is True


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
    assert data["error"]["details"]["field"] == "workspace_root"


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
    assert "field" not in data["error"].get("details", {})
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
    assert data["error"]["details"]["field"] == "workspace_root"


async def test_review_changes_async_lifecycle(monkeypatch, git_repo, tmp_path):
    # End-to-end through the MCP surface: launch async -> poll status -> get the
    # same envelope as the sync tool. build_command is replaced with a fake that
    # writes a known claude envelope, so no real CLI runs.
    import json as _json

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
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], []),
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

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        claude_mod,
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
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["claude"], []))

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
            {"job_id": "d" * 32, "workspace_root": str(git_repo)},
            raise_on_error=False,
        )
    data = structured(result)
    assert data["ok"] is False
    assert data["error"]["code"] == "job_not_found"


async def test_job_consume_result_deletes_finished_record(monkeypatch, git_repo, tmp_path):
    import json as _json
    import time as _time

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
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], []),
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
    assert data["fingerprint"] == "claude-in-codex/0.1/schema-37"
    assert data["transport"] == "stdio"
    assert set(data["paid_tools"]) == {
        "claude_consult",
        "claude_review_changes",
        "claude_adversarial_review",
        "claude_review_changes_async",
        "claude_consult_async",
        "claude_adversarial_review_async",
        # Deprecated alias of claude_consult; removal planned for 0.9.0.
        "claude_ask",
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


async def test_dry_run_envelopes_echo_the_invoked_name(monkeypatch, git_repo):
    """Request name and envelope `tool` must agree, for BOTH registered names."""
    monkeypatch.chdir(git_repo)
    for name in ("claude_dry_run", "claude_review_dry_run"):
        async with Client(mcp) as client:
            data = structured(
                await client.call_tool(
                    name, {"scope": "working_tree", "workspace_root": str(git_repo)}
                )
            )
        assert data["tool"] == name


async def test_dry_run_alias_input_schema_is_identical():
    """The alias promises identical parameters. Pin that the split did not
    change either signature."""
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["claude_dry_run"].inputSchema == tools["claude_review_dry_run"].inputSchema


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
    assert data["error"]["details"]["field"] == "config_mode"


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

    _sp.run(["git", "checkout", "--", "app.py"], cwd=git_repo, check=True)
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        claude_mod,
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
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], []),
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
    assert data["error"]["details"]["field"] == "base"


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
    assert data["meta"]["effective_max_budget_usd"] == 0.25
    assert "configured_max_budget_usd" not in data["meta"]


async def test_meta_distinguishes_raw_env_budget_from_effective_budget(
    fake_claude, monkeypatch, git_repo
):
    monkeypatch.setenv("CLAUDE_IN_CODEX_MAX_BUDGET_USD", "99")
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool("claude_ask", {"prompt": "x", "workspace_root": str(git_repo)})
        )
    assert "requested_max_budget_usd" not in data["meta"]
    assert data["meta"]["configured_max_budget_usd"] == 99.0
    assert data["meta"]["effective_max_budget_usd"] == 5.0


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
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], []),
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
    assert data["error"]["retryable"] is False
    assert "$0.10-$0.20" in data["error"]["repair"]
    assert data["meta"]["cost_usd"] == 0.05
    assert data["meta"]["usage"]["input_tokens"] == 10


@pytest.mark.parametrize(
    "tool,args",
    [
        ("claude_ask", {"prompt": "x"}),
        ("claude_adversarial_review", {"target": "x"}),
        ("claude_review_changes_async", {"scope": "working_tree"}),
        ("claude_consult_async", {"prompt": "x"}),
        ("claude_adversarial_review_async", {"target": "x"}),
        ("claude_job_status", {"job_id": "d" * 32}),
        ("claude_job_result", {"job_id": "d" * 32}),
        ("claude_job_consume_result", {"job_id": "d" * 32}),
        ("claude_job_cancel", {"job_id": "d" * 32}),
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
                {"job_id": "d" * 32, "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
        cancel = structured(
            await client.call_tool(
                "claude_job_cancel",
                {"job_id": "d" * 32, "workspace_root": str(git_repo)},
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
        ("claude_adversarial_review_async", {"target": "x", "scope": "working_tree"}),
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
        ("claude_adversarial_review_async", {"target": "x", "scope": "working_tree"}),
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
        ("claude_adversarial_review_async", {"target": "x", "scope": "working_tree"}),
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
        ("claude_adversarial_review_async", {"target": "x", "scope": "working_tree"}),
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
    assert data["error"]["details"]["field"] == "base"


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

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
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
    assert data["error"]["details"]["field"] == "head"


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
    assert data["error"]["details"]["field"] == "head"


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
    assert data["error"]["details"]["field"] == "head"


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
    assert data["error"]["details"]["field"] == "head"


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
    assert data["error"]["details"]["field"] == "head"


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
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], []),
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


def _call_validation_error(title: str = "call[claude_ask]") -> PydanticValidationError:
    """A pydantic ValidationError shaped like FastMCP's call-adapter failure."""
    model = create_model(title, effort=(Literal["low", "high"], ...))
    with pytest.raises(PydanticValidationError) as excinfo:
        model(effort="bogus")
    return excinfo.value


def test_argument_error_unwraps_both_fastmcp_raise_forms():
    """The envelope must survive either FastMCP argument-validation raise form.

    FastMCP <= 3.4.2 lets the call adapter's pydantic ValidationError escape;
    3.4.3 and later wrap it in fastmcp's own ValidationError (#4128). Neither
    form is version-detectable at runtime, so both are asserted here rather than
    only through whichever FastMCP the test environment installs."""
    bare = _call_validation_error()
    wrapped = FastMCPValidationError(str(bare))
    wrapped.__cause__ = bare

    assert ValidationEnvelopeMiddleware._argument_error(bare) is bare
    assert ValidationEnvelopeMiddleware._argument_error(wrapped) is bare


def test_argument_error_ignores_a_tool_body_model_error():
    """A tool body's own model error is an internal bug, not a bad call.

    Its title is the model class name, not `call[...]`, in both the bare and the
    wrapped form — so it must propagate instead of becoming an
    invalid_arguments envelope addressed to the caller."""
    body = _call_validation_error(title="SomeInternalModel")
    wrapped = FastMCPValidationError(str(body))
    wrapped.__cause__ = body

    assert ValidationEnvelopeMiddleware._argument_error(body) is None
    assert ValidationEnvelopeMiddleware._argument_error(wrapped) is None


async def test_invalid_enum_argument_returns_envelope():
    async with Client(mcp) as client:
        res = await client.call_tool(
            "claude_review_dry_run", {"scope": "bogus"}, raise_on_error=False
        )
    assert res.is_error is True
    payload = structured(res)
    assert payload["ok"] is False
    err = payload["error"]
    assert err["code"] == "invalid_arguments"
    assert err["details"]["field"] == "scope"
    assert "working_tree" in err["repair"]


@pytest.mark.parametrize("max_budget_usd", [0.0, -1.0, 5.01])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("claude_ask", {"prompt": "x"}),
        ("claude_review_changes", {"scope": "working_tree"}),
        ("claude_adversarial_review", {"target": "x"}),
        ("claude_review_changes_async", {"scope": "working_tree"}),
    ],
)
async def test_out_of_range_budget_is_rejected_before_paid_runner(
    monkeypatch, tool, arguments, max_budget_usd
):
    import claude_in_codex.server as srv

    async def fail_sync_runner(*args, **kwargs):
        pytest.fail("out-of-range budget reached the synchronous paid runner")

    def fail_async_runner(*args, **kwargs):
        pytest.fail("out-of-range budget reached the background paid runner")

    monkeypatch.setattr(srv, "run_claude_async", fail_sync_runner)
    monkeypatch.setattr(srv.jobs, "start_job", fail_async_runner)
    async with Client(mcp) as client:
        result = await client.call_tool(
            tool,
            {**arguments, "max_budget_usd": max_budget_usd},
            raise_on_error=False,
        )
    assert result.is_error is True
    error = structured(result)["error"]
    assert error["code"] == "invalid_arguments"
    assert error["details"]["field"] == "max_budget_usd"


@pytest.mark.parametrize("max_budget_usd", [0.0, -1.0, 5.01])
def test_resolve_defensively_rejects_out_of_range_budget(max_budget_usd, tmp_path):
    import claude_in_codex.server as srv

    resolved, error = srv._resolve(
        None,
        None,
        None,
        max_budget_usd,
        None,
        "summary",
        str(tmp_path),
    )
    assert resolved is None
    assert error["error"]["code"] == "invalid_arguments"
    assert error["error"]["details"]["field"] == "max_budget_usd"
    assert error["meta"]["requested_max_budget_usd"] == max_budget_usd
    assert "effective_max_budget_usd" not in error["meta"]


@pytest.mark.parametrize("job_id", ["../outside", "/tmp/outside", "A" * 32, "a" * 31])
async def test_invalid_job_id_returns_validation_envelope(job_id):
    async with Client(mcp) as client:
        res = await client.call_tool("claude_job_status", {"job_id": job_id}, raise_on_error=False)
    assert res.is_error is True
    err = structured(res)["error"]
    assert err["code"] == "invalid_arguments"
    assert err["details"]["field"] == "job_id"


async def test_missing_required_argument_returns_envelope():
    async with Client(mcp) as client:
        res = await client.call_tool("claude_review_dry_run", {}, raise_on_error=False)
    assert res.is_error is True
    err = structured(res)["error"]
    assert err["code"] == "invalid_arguments"
    assert err["details"]["field"] == "scope"
    # A missing argument has no rejected value; pydantic reports the whole
    # arguments dict there, which would name every argument as the offender.
    assert "value" not in err["details"]


async def test_invalid_enum_argument_carries_allowed_values():
    async with Client(mcp) as client:
        res = await client.call_tool(
            "claude_review_dry_run", {"scope": "bogus"}, raise_on_error=False
        )
    err = structured(res)["error"]
    assert err["code"] == "invalid_arguments"
    assert err["details"]["allowed_values"] == ["working_tree", "staged", "branch"]


def test_capability_summary_names_error_carrier():
    assert "structuredContent" in CAPABILITY_SUMMARY
    assert "ok:false" in CAPABILITY_SUMMARY


async def test_validation_middleware_reraises_internal_model_errors():
    from pydantic import BaseModel, ValidationError

    from claude_in_codex.server import ValidationEnvelopeMiddleware

    class Inner(BaseModel):
        x: int

    async def call_next(context):
        Inner(x="nope")  # internal model bug, not argument coercion

    with pytest.raises(ValidationError):
        await ValidationEnvelopeMiddleware().on_call_tool(None, call_next)


def test_invalid_scope_error_carries_allowed_values():
    from claude_in_codex.server import _invalid_scope_error, _meta

    payload = _invalid_scope_error(_meta("", "inherit", "toolless", 0, 0, None), "bogus")
    err = payload["error"]
    assert err["details"]["allowed_values"] == ["working_tree", "staged", "branch"]


async def test_job_not_found_carries_repair_tool(tmp_path):
    async with Client(mcp) as client:
        res = await client.call_tool(
            "claude_job_status",
            {"job_id": "d" * 32, "workspace_root": str(tmp_path)},
            raise_on_error=False,
        )
    err = structured(res)["error"]
    assert err["code"] == "job_not_found"
    assert err["action"]["tool"] == "claude_job_list"


# --- first-repair contract (#60) ---------------------------------------------
# Each of these asserts the error is repairable on the FIRST attempt: the agent
# can act on `action` (and `details`) without parsing prose or guessing arguments.


async def test_invalid_enum_repair_rebuilds_the_call_minus_the_bad_field():
    async with Client(mcp) as client:
        res = await client.call_tool(
            "claude_review_dry_run",
            {"scope": "bogus", "base": "main", "paths": ["src"]},
            raise_on_error=False,
        )
    err = structured(res)["error"]
    action = err["action"]
    assert err["retryable"] is False  # the identical call can never succeed
    assert action["next_step"] == "retry_with_changes"
    assert action["tool"] == "claude_review_dry_run"
    # Every still-valid argument survives; only the invalid one is dropped.
    assert action["arguments"] == {"base": "main", "paths": ["src"]}
    assert err["details"]["value"] == "bogus"


async def test_oversized_repair_arguments_are_omitted_not_echoed(monkeypatch):
    """A giant prompt must not come back inside the repair block."""
    from claude_in_codex.server import REPAIR_ARGS_MAX_BYTES

    async with Client(mcp) as client:
        res = await client.call_tool(
            "claude_ask",
            {"prompt": "x" * (REPAIR_ARGS_MAX_BYTES + 1), "effort": "bogus"},
            raise_on_error=False,
        )
    err = structured(res)["error"]
    assert err["action"]["next_step"] == "retry_with_changes"
    assert err["action"]["tool"] == "claude_ask"
    assert "arguments" not in err["action"]


async def test_job_not_found_repair_pins_the_resolved_workspace(tmp_path):
    async with Client(mcp) as client:
        res = await client.call_tool(
            "claude_job_status",
            {"job_id": "d" * 32, "workspace_root": str(tmp_path)},
            raise_on_error=False,
        )
    err = structured(res)["error"]
    assert err["action"]["next_step"] == "call_tool"
    assert err["action"]["tool"] == "claude_job_list"
    # Listing under a differently-resolved workspace would show an unrelated set.
    assert err["action"]["arguments"] == {"workspace_root": str(tmp_path)}
    assert err["details"] == {"field": "job_id", "value": "d" * 32, "reason": "unknown_or_expired"}


async def test_oversized_user_input_reports_typed_sizes(monkeypatch):
    # 1_000 is max_input_bytes' hard floor, so it is the smallest testable cap.
    monkeypatch.setenv("CLAUDE_IN_CODEX_MAX_INPUT_BYTES", "1000")
    async with Client(mcp) as client:
        res = await client.call_tool("claude_ask", {"prompt": "y" * 2000}, raise_on_error=False)
    err = structured(res)["error"]
    assert err["code"] == "context_too_large"
    details = err["details"]
    assert details["limit_bytes"] == 1000
    assert details["actual_bytes"] == 2000
    assert details["field"] == "prompt"


async def test_oversized_diff_reports_typed_sizes(monkeypatch, git_repo):
    import claude_in_codex.context as ctx_mod
    import claude_in_codex.server as srv

    monkeypatch.setattr(ctx_mod, "MAX_DIFF_BYTES", 32)
    monkeypatch.setattr(srv, "MAX_DIFF_BYTES", 32)
    (git_repo / "big.txt").write_text("z" * 5000)
    async with Client(mcp) as client:
        res = await client.call_tool(
            "claude_review_changes",
            {"scope": "working_tree", "workspace_root": str(git_repo)},
            raise_on_error=False,
        )
    err = structured(res)["error"]
    assert err["code"] == "context_too_large"
    assert err["details"]["max_diff_bytes"] == 32
    assert err["details"]["diff_bytes"] > 32


async def test_workspace_outside_roots_publishes_the_allowed_roots(fake_claude, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    async with Client(mcp, roots=[root.as_uri()]) as client:
        res = await client.call_tool(
            "claude_review_changes",
            {"scope": "working_tree", "workspace_root": str(outside)},
            raise_on_error=False,
        )
    err = structured(res)["error"]
    assert err["details"]["allowed_roots"] == [str(root)]
    # The corrected call is spelled out, not left for the agent to infer.
    assert err["action"]["arguments"] == {"workspace_root": str(root)}


def test_every_error_carries_exactly_one_next_step():
    """`action` is total: no error can leave the caller without a branch."""
    from claude_in_codex.schemas import ErrorInfo

    for code in get_args(ErrorCode):
        info = ErrorInfo(code=code, message="m", repair="r")
        assert info.action is not None and info.action.next_step


def test_retryable_never_pairs_with_a_changed_call():
    """retryable=True means the IDENTICAL call may succeed — never a corrected one."""
    from claude_in_codex.schemas import DEFAULT_NEXT_STEP, ErrorInfo

    assert ErrorInfo(code="timeout", message="m", repair="r", retryable=True).action.next_step == (
        "retry_same_call"
    )
    for code, _cond, ever_retryable, _fields in _ERROR_CATALOG:
        if DEFAULT_NEXT_STEP[code] == "retry_with_changes":
            assert not ever_retryable, f"{code} says a changed call is also a plain retry"


def test_published_next_step_is_the_one_errors_actually_carry():
    """The catalog is a claim about emitted errors; check it against real ones.

    Without this the documented default and the envelope's default are two
    hand-maintained copies, and the published one is the copy nobody exercises."""
    from claude_in_codex.schemas import ErrorInfo

    published = {row["code"]: row["next_step"] for row in _capabilities_payload()["error_catalog"]}
    for code in get_args(ErrorCode):
        emitted = ErrorInfo(code=code, message="m", repair="r").action.next_step
        assert published[code] == emitted, f"{code}: documents {published[code]}, emits {emitted}"


def test_error_catalog_covers_every_code_exactly_once():
    documented = [row[0] for row in _ERROR_CATALOG]
    assert sorted(documented) == sorted(get_args(ErrorCode))
    assert len(documented) == len(set(documented))


def test_catalog_detail_fields_exist_on_error_details():
    from claude_in_codex.schemas import ErrorDetails

    for code, _cond, _retryable, fields in _ERROR_CATALOG:
        unknown = set(fields) - set(ErrorDetails.model_fields)
        assert not unknown, f"{code} documents non-existent detail fields {unknown}"


def test_per_tool_error_codes_cover_the_statically_reachable_ones():
    """The published branch map must not under-report what the code can raise.

    A static walk of server.py resolves the literal codes each tool's own call
    graph can produce; every one of those must appear in that tool's published
    set. Codes raised outside server.py (claude.py's CLI classification, jobs.py's
    lifecycle) are not visible to this walk, so it is a floor, not a ceiling — but
    it is a floor that moves whenever someone adds an error path and forgets the
    map."""
    reachable = _statically_reachable_error_codes()
    # Instrument check: a walk that resolves nothing would make the loop below
    # vacuously pass, so pin codes each tool demonstrably raises in server.py.
    assert "invalid_scope" in reachable["claude_dry_run"]
    assert "context_too_large" in reachable["claude_consult"]
    assert "job_not_found" in reachable["claude_job_status"]
    for tool, codes in reachable.items():
        missing = codes - set(_TOOL_ERROR_CODES[tool])
        assert not missing, f"{tool} can raise {sorted(missing)} but does not publish them"


def test_async_starter_does_not_advertise_completion_time_errors():
    """The starter returns a job handle, so a code that can only be produced by a
    finished claude run must come from the result tools, not from the start call."""
    from claude_in_codex.server import _CLAUDE_ERRORS

    starter = set(_TOOL_ERROR_CODES["claude_review_changes_async"])
    # claude_not_found is the exception: it fails before any job is spawned.
    assert starter & set(_CLAUDE_ERRORS) == {"claude_not_found"}
    for fetcher in ("claude_job_result", "claude_job_consume_result"):
        assert set(_CLAUDE_ERRORS) <= set(_TOOL_ERROR_CODES[fetcher])


def test_per_tool_error_codes_are_all_documented():
    catalog = {row[0] for row in _ERROR_CATALOG}
    for tool, codes in _TOOL_ERROR_CODES.items():
        assert set(codes) <= catalog, f"{tool} publishes codes missing from the catalog"


def test_published_tool_names_match_the_error_map():
    payload = _capabilities_payload()
    # Every advertised tool has a branch map, including the ones without a
    # tool_details entry — otherwise a tool could ship with no documented errors.
    assert set(_TOOL_ERROR_CODES) == set(payload["paid_tools"]) | set(payload["free_tools"])
    assert {t["name"] for t in payload["tool_details"]} <= set(_TOOL_ERROR_CODES)


def test_capabilities_publishes_the_async_lifecycle():
    lifecycle = _capabilities_payload()["async_lifecycle"]
    tools = {t["name"] for t in _capabilities_payload()["tool_details"]}
    for field in ("status_tool", "result_tool", "consume_tool", "cancel_tool", "list_tool"):
        assert lifecycle[field] in tools, f"{field} names an unregistered tool"
    assert set(lifecycle["start_tools"]) <= tools
    assert set(lifecycle["nonresult_terminal_codes"]) <= set(get_args(ErrorCode))
    assert set(lifecycle["terminal_states"]) | set(lifecycle["running_states"]) == set(
        get_args(JobState)
    )


async def test_async_same_idempotency_key_returns_existing_job(git_repo, monkeypatch):

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    args = {
        "scope": "working_tree",
        "workspace_root": str(git_repo),
        "idempotency_key": "key-1",
    }
    async with Client(mcp) as client:
        first = structured(await client.call_tool("claude_review_changes_async", args))
        second = structured(await client.call_tool("claude_review_changes_async", args))
        await client.call_tool(
            "claude_job_cancel",
            {"job_id": first["job_id"], "workspace_root": str(git_repo)},
        )
    assert first["ok"] is True
    assert second["job_id"] == first["job_id"]
    assert second["status"] == "running"


async def test_async_same_key_different_args_is_a_conflict(git_repo, monkeypatch):
    """The store's idempotency index dedupes on (key, effective arguments): the
    same key with DIFFERENT arguments is a conflict, never a silent replay of a
    run the caller did not ask for. (0.7 deduped on the key alone.)"""

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    async with Client(mcp) as client:
        first = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "key-2",
                },
            )
        )
        second = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "paths": ["app.py"],
                    "workspace_root": str(git_repo),
                    "idempotency_key": "key-2",
                },
                raise_on_error=False,
            )
        )
        await client.call_tool(
            "claude_job_cancel",
            {"job_id": first["job_id"], "workspace_root": str(git_repo)},
        )
    assert second["ok"] is False
    assert second["error"]["code"] == "idempotency_conflict"
    assert second["error"]["details"]["field"] == "idempotency_key"


async def test_async_same_key_same_args_replays_existing_job(git_repo, monkeypatch):
    """Identical key + identical effective arguments is the replay case: the
    caller gets the existing job's status instead of a second paid launch."""

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    async with Client(mcp) as client:
        first = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "key-replay",
                },
            )
        )
        second = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "key-replay",
                },
            )
        )
        await client.call_tool(
            "claude_job_cancel",
            {"job_id": first["job_id"], "workspace_root": str(git_repo)},
        )
    assert second["job_id"] == first["job_id"]


async def test_async_reservation_contention_returns_existing_job(git_repo, monkeypatch):
    """Force both launches past the cheap entry fast-path (by making it always
    report no match) so the second launch's dedupe is resolved entirely by the
    atomic pre-spawn reservation, not the advisory find_by_idempotency_key scan."""
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    monkeypatch.setattr(srv.jobs, "find_by_idempotency_key", lambda *a, **k: None)
    args = {
        "scope": "working_tree",
        "workspace_root": str(git_repo),
        "idempotency_key": "contention-key",
    }
    async with Client(mcp) as client:
        first = structured(await client.call_tool("claude_review_changes_async", args))
        second = structured(await client.call_tool("claude_review_changes_async", args))
        await client.call_tool(
            "claude_job_cancel",
            {"job_id": first["job_id"], "workspace_root": str(git_repo)},
        )
    assert first["ok"] is True
    assert second["job_id"] == first["job_id"]


async def test_async_legacy_ghost_marker_does_not_block_a_fresh_launch(git_repo, monkeypatch):
    """A legacy (0.7) marker whose job record never landed is ignored — the keyed
    launch proceeds fresh through the store index instead of erroring."""
    from claude_in_codex import jobs

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    marker = jobs._reservation_path(str(git_repo), "ghost-key")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"job_id": "f" * 32, "created_epoch": time.time()}))
    async with Client(mcp) as client:
        result = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "ghost-key",
                },
            )
        )
        await client.call_tool(
            "claude_job_cancel",
            {"job_id": result["job_id"], "workspace_root": str(git_repo)},
        )
    assert result["ok"] is True
    assert result["job_id"] != "f" * 32


async def test_async_spawn_failure_releases_reservation(monkeypatch, git_repo, tmp_path):
    """On a spawn failure the store rolls the reservation back, so a retry with
    the same key launches fresh instead of being stuck behind a phantom."""

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        claude_mod, "build_command", lambda *a, **k: (["definitely-no-such-claude-binary-xyz"], [])
    )
    async with Client(mcp) as client:
        result = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "release-key",
                },
                raise_on_error=False,
            )
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "claude_not_found"

        # Reservation rolled back: the same key now launches successfully.
        monkeypatch.setattr(
            claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], [])
        )
        retry = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "release-key",
                },
            )
        )
        assert retry["ok"] is True
        await client.call_tool(
            "claude_job_cancel",
            {"job_id": retry["job_id"], "workspace_root": str(git_repo)},
        )


async def test_async_oserror_spawn_failure_is_internal_error(monkeypatch, git_repo, tmp_path):
    import claude_in_codex.server as srv

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["claude"], []))

    def fake_start(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(srv.jobs, "start_job_idempotent", fake_start)
    async with Client(mcp) as client:
        result = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "release-key-2",
                },
                raise_on_error=False,
            )
        )
    assert result["ok"] is False
    assert result["error"]["code"] == "internal_error"


async def test_annotation_contract():
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    # Paid tools spend money and send context to Anthropic: not read-only,
    # destructive in the worst config-mode case because workspace hooks may run
    # arbitrary shell, non-idempotent (each call spends), open-world.
    for name in PAID_TOOLS:
        ann = tools[name].annotations
        assert ann.readOnlyHint is False, name
        assert ann.destructiveHint is True, name
        assert ann.idempotentHint is False, name
        assert ann.openWorldHint is True, name
    # Job polling performs lazy maintenance while reading (deadline kills, TTL
    # deletion): not read-only, but never alters a terminal job's stored result.
    for name in ("claude_job_status", "claude_job_result", "claude_job_list"):
        assert tools[name].annotations.readOnlyHint is False, name
    # Consume irreversibly deletes the stored result record.
    consume = tools["claude_job_consume_result"].annotations
    assert consume.readOnlyHint is False
    assert consume.destructiveHint is True
    # Cancel is idempotent: already-terminal jobs are returned unchanged.
    cancel = tools["claude_job_cancel"].annotations
    assert cancel.readOnlyHint is False
    assert cancel.idempotentHint is True
    # Pure reads: no spend, no job-lifecycle side effects.
    for name in ("claude_status", "claude_capabilities", "claude_models", "claude_review_dry_run"):
        ann = tools[name].annotations
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is None, name
        assert ann.idempotentHint is None, name
    assert tools["claude_status"].annotations.openWorldHint is False


def test_capabilities_documents_annotations_policy():
    policy = _capabilities_payload()["annotations_policy"]
    assert "readOnlyHint" in policy
    assert "destructiveHint is true" in policy
    assert "worst case across config modes" in policy
    assert "workspace hooks" in policy
    for mode in ("inherit", "scoped", "safe", "bare"):
        assert f"config_mode={mode}" in policy
    assert "lazy maintenance" in policy


async def test_capabilities_publishes_the_detail_contract():
    """The paid tools advertise only a pointer, so this must be the real home (#94).

    Guards the failure the pointer would otherwise create: a `detail` description
    that names claude_capabilities.detail_modes while that field says nothing
    actionable about caps, subsetting, or recovery."""
    async with Client(mcp) as client:
        data = structured(await client.call_tool("claude_capabilities", {}))
    modes = data["detail_modes"]
    assert modes["levels"] == ["summary", "full"]
    assert modes["default"] == "summary"
    assert set(modes["full_only_fields"]) == {"raw_response.text", "context_summary"}
    assert modes["truncation_marker"] == TRUNCATION_MARKER
    # Every published cap must match the bounds the server actually enforces.
    for level, bounds in OUTPUT_BOUNDS.items():
        assert modes["bounds"][level] == bounds.model_dump(mode="json")
    # summary must be the tighter level on every cap that is live at both levels.
    summary, full = modes["bounds"]["summary"], modes["bounds"]["full"]
    for cap in summary:
        if cap == "max_raw_text_chars":
            continue  # inert at summary, which omits raw_response.text entirely
        assert summary[cap] < full[cap], cap
    # The prose is the only home for the truncation block's shape and recovery,
    # including the two documented exclusions from the subset claim and the
    # consume-mode rule that keeps a deleted record from being advertised.
    for required in (
        "truncation{",
        "claude_job_result",
        "NEW PAID CALL",
        "workspace_root",
        "claude_job_consume_result",
        "only the occurrences that were actually shortened",
    ):
        assert required in modes["truncation"], required


async def test_paid_tool_detail_params_point_at_the_capability_contract():
    tools = await _tools_by_name()
    for name in (
        "claude_ask",
        "claude_review_changes",
        "claude_adversarial_review",
        "claude_review_changes_async",
    ):
        described = tools[name].inputSchema["properties"]["detail"]["description"]
        assert "claude_capabilities.detail_modes" in described, name
    for name in ("claude_job_result", "claude_job_consume_result"):
        assert "detail" in tools[name].inputSchema["properties"], name


async def test_job_result_accepts_a_detail_override_over_mcp(monkeypatch, git_repo, tmp_path):
    """The next step a truncated job result hands back must be literally callable."""
    import json as _json
    import time as _time

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    inner = {"summary": "ok", "verdict": "pass", "confidence": "high", "findings": []}
    envelope = _json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": _json.dumps(inner)}
    )
    monkeypatch.setattr(
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], []),
    )

    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo), "detail": "summary"},
            )
        )
        job_id = started["job_id"]
        deadline = _time.time() + 5
        while _time.time() < deadline:
            status = structured(
                await client.call_tool(
                    "claude_job_status", {"job_id": job_id, "workspace_root": str(git_repo)}
                )
            )
            if status["status"] == "done":
                break
            await anyio.sleep(0.05)
        assert status["status"] == "done"
        summary = structured(
            await client.call_tool(
                "claude_job_result", {"job_id": job_id, "workspace_root": str(git_repo)}
            )
        )
        # These are exactly the arguments a truncated summary's truncation block
        # publishes for recovery.
        full = structured(
            await client.call_tool(
                "claude_job_result",
                {"job_id": job_id, "detail": "full", "workspace_root": str(git_repo)},
            )
        )
    assert summary["ok"] is True
    assert "text" not in summary["raw_response"]
    assert full["ok"] is True
    assert full["raw_response"]["text"]


async def test_initialize_reports_application_version_and_name():
    """serverInfo must identify the application, not the MCP framework (#89).

    Hosts read initialize metadata for diagnostics, caching, and compatibility
    decisions, so a FastMCP upgrade must not look like an application release.
    Pinning the whole serverInfo block (not just the version) also catches a
    silent framework-added field or a name drift away from claude_capabilities.
    """
    async with Client(mcp) as client:
        server_info = client.initialize_result.serverInfo.model_dump(mode="json", exclude_none=True)
        instructions = client.initialize_result.instructions
    capabilities = _capabilities_payload()

    assert server_info == {"name": "claude-in-codex", "version": __version__}
    assert server_info["name"] == capabilities["name"]
    assert server_info["version"] == capabilities["version"]
    assert instructions == CAPABILITY_SUMMARY


async def test_deprecated_aliases_match_their_primaries(fake_claude):
    """Both deprecated aliases take the same arguments and publish the same
    schemas as their primaries. Removal planned for 0.9.0.

    The envelope claim holds for claude_ask only, and that is deliberate: a
    dry-run envelope echoes the NAME the caller invoked, so claude_review_dry_run
    and claude_dry_run differ in exactly that one field. This test asserts full
    envelope equality for the consult pair and schema equality for both pairs;
    test_dry_run_envelopes_echo_the_invoked_name owns the difference.
    """
    async with Client(mcp) as client:
        primary = structured(await client.call_tool("claude_consult", {"prompt": "is this safe?"}))
        alias = structured(await client.call_tool("claude_ask", {"prompt": "is this safe?"}))
    # Identical modulo the genuinely per-call meta fields.
    for envelope in (primary, alias):
        for per_call in ("request_id", "elapsed_ms"):
            envelope["meta"].pop(per_call, None)
    assert alias == primary

    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["claude_ask"].inputSchema == tools["claude_consult"].inputSchema
    assert tools["claude_ask"].outputSchema == tools["claude_consult"].outputSchema
    assert tools["claude_review_dry_run"].inputSchema == tools["claude_dry_run"].inputSchema
    assert tools["claude_review_dry_run"].outputSchema == tools["claude_dry_run"].outputSchema


@pytest.mark.parametrize(
    ("outcome_kind", "expected_code", "expected_retryable"),
    [
        ("unavailable", "idempotency_result_unavailable", False),
        ("in_progress", "idempotency_in_progress", True),
        # NOT idempotency_in_progress: an I/O failure in the index does not
        # establish that any concurrent launch exists, so telling the caller a
        # winner will be replayed would be a fabricated cause.
        ("io_error", "internal_error", True),
    ],
)
async def test_async_idempotency_coordination_outcomes_map_to_published_codes(
    git_repo, monkeypatch, outcome_kind, expected_code, expected_retryable
):
    """The store's non-create, non-replay, non-conflict outcomes are agent-facing.

    `unavailable`, `in_progress`, and `io_error` each reach the caller as a
    published error code with a repair, and only the recoverable ones are
    retryable. Nothing else exercised these branches, so a remapping could
    change the wire silently.

    `io_error` maps to internal_error rather than idempotency_in_progress: the
    index failed to read or write, which says nothing about a concurrent
    launch, so reporting one would state a cause that was never established.
    """
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    monkeypatch.setattr(jobs_mod, "start_job_idempotent", lambda *a, **k: {"kind": outcome_kind})
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": f"coord-{outcome_kind}",
                },
                raise_on_error=False,
            )
        )
    assert out["ok"] is False
    assert out["error"]["code"] == expected_code
    assert out["error"]["retryable"] is expected_retryable
    assert out["error"]["repair"]
    assert out["error"]["details"]["field"] == "idempotency_key"


async def test_async_idempotency_replay_without_a_record_is_an_internal_error(
    git_repo, monkeypatch
):
    """A published replay whose job record is gone must not be reported as a
    successful launch. This branch had no coverage either."""
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    monkeypatch.setattr(
        jobs_mod,
        "start_job_idempotent",
        lambda *a, **k: {"kind": "replay", "job_id": "0" * 32},
    )
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "replay-no-record",
                },
                raise_on_error=False,
            )
        )
    assert out["ok"] is False
    assert out["error"]["code"] == "internal_error"
    assert out["error"]["retryable"] is True


async def test_legacy_idempotency_marker_fails_closed_instead_of_replaying(git_repo, monkeypatch):
    """A 0.7 marker cannot honor the (key, effective arguments) guarantee.

    0.7 markers carry no arg_hash, so replaying one is an UNVERIFIED match: the
    caller could have changed scope, paths, model, effort, or focus and would
    silently receive the earlier job's answer, which the published contract now
    promises is an idempotency_conflict. Markers are read-only legacy state and
    the TTL is 24h, so this window closes on its own; until it does, refusing
    costs at most one extra launch and never misattributes a paid answer.
    """
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))

    cwd = str(git_repo)
    cfg = jobs_mod.JobConfig(
        kind="claude_review_changes",
        config_mode="inherit",
        access="toolless",
        scope="working_tree",
        base="main",
        head=None,
        detail="summary",
        timeout_seconds=1800,
        workspace_source="cwd",
        context_summary=None,
    )
    job_id, _ = jobs_mod.start_job(["sh", "-c", "sleep 30"], cwd, cfg)
    marker = jobs_mod._reservation_path(cwd, "legacy-key")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"job_id": job_id, "created_epoch": time.time()}))

    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": cwd,
                    "idempotency_key": "legacy-key",
                },
                raise_on_error=False,
            )
        )
    jobs_mod.cancel(cwd, job_id)

    assert out["ok"] is False
    assert out["error"]["code"] == "idempotency_conflict"
    assert out["error"]["details"]["field"] == "idempotency_key"
    # The job itself stays reachable by id — refusing the key must not orphan it.
    assert jobs_mod.status(cwd, job_id) is not None


async def test_a_key_with_no_legacy_marker_still_launches(git_repo, monkeypatch):
    """Positive control: the refusal is specific to legacy markers, not to keys."""
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                    "idempotency_key": "fresh-key",
                },
                raise_on_error=False,
            )
        )
        assert out["ok"] is True
        await client.call_tool(
            "claude_job_cancel",
            {"job_id": out["job_id"], "workspace_root": str(git_repo)},
        )


async def test_an_expired_legacy_marker_stops_blocking_the_key(git_repo, monkeypatch):
    """The 24h compatibility window has to actually close.

    The refusal is only defensible because it expires: markers are never
    written and terminal records are reaped at the TTL. But the store reaps
    LAZILY, on a store call, and the legacy check is a fast return that happens
    before `start_job_idempotent` would trigger one. Resolving the marker
    without refreshing the record left an expired job blocking its key forever
    — a permanent refusal wearing a temporary one's justification.

    The live-record refusal
    (test_legacy_idempotency_marker_fails_closed_instead_of_replaying) is the
    positive control: it proves this path refuses at all, so a launch here is
    not a broken check passing everything through.
    """
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))

    cwd = str(git_repo)
    cfg = jobs_mod.JobConfig(
        kind="claude_review_changes",
        config_mode="inherit",
        access="toolless",
        scope="working_tree",
        base="main",
        head=None,
        detail="summary",
        timeout_seconds=1800,
        workspace_source="cwd",
        context_summary=None,
    )
    job_id, _ = jobs_mod.start_job(["sh", "-c", "exit 0"], cwd, cfg)
    for _ in range(50):
        if jobs_mod.status(cwd, job_id)["status"] != "running":
            break
        time.sleep(0.05)
    marker = jobs_mod._reservation_path(cwd, "expired-key")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"job_id": job_id, "created_epoch": time.time()}))
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_TTL", "0")  # every terminal record is expired

    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {
                    "scope": "working_tree",
                    "workspace_root": cwd,
                    "idempotency_key": "expired-key",
                },
                raise_on_error=False,
            )
        )
        assert out["ok"] is True, out
        await client.call_tool(
            "claude_job_cancel", {"job_id": out["job_id"], "workspace_root": cwd}
        )


# --------------------------------------------------------------- #93: no paid
# call is blocking-only. Every paid tool now has a recoverable execution path,
# so a cancelled or disconnected call no longer loses work it already paid for.


def _fake_envelope(inner: dict, cost: float = 0.02) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(inner),
            "total_cost_usd": cost,
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
    )


_INNER = {
    "summary": "the plan assumes a single writer",
    "verdict": "concerns",
    "confidence": "high",
    "findings": [],
    "questions": [],
    "assumptions": [],
}


async def _drain(client, job_id, cwd, timeout=5.0):
    """Poll one job to a terminal state and return its final status payload."""
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = structured(
            await client.call_tool("claude_job_status", {"job_id": job_id, "workspace_root": cwd})
        )
        if st["status"] != "running":
            return st
        await anyio.sleep(0.05)
    raise AssertionError(f"job {job_id} never left running: {st}")


def _capture_prompts(monkeypatch) -> list[str]:
    """Record the prompt each job would send, and answer with a fixed envelope."""
    seen: list[str] = []

    def fake_build_command(prompt, *a, **k):
        seen.append(prompt)
        return (["sh", "-c", "printf '%s' \"$0\"", _fake_envelope(_INNER)], [])

    monkeypatch.setattr(claude_mod, "build_command", fake_build_command)
    return seen


async def test_every_paid_tool_has_a_recoverable_execution_path():
    """#93's acceptance criterion, asserted rather than documented.

    A blocking paid call that is cancelled or loses its connection loses the
    spend; a job survives both. Every paid tool must therefore either BE an async
    starter or have one, so a new paid tool cannot ship blocking-only by
    omission. Deprecated aliases inherit their primary's async form.
    """
    data = _capabilities_payload()
    starters = set(data["async_lifecycle"]["start_tools"])
    aliases = {"claude_ask": "claude_consult"}
    for tool in data["paid_tools"]:
        canonical = aliases.get(tool, tool)
        assert canonical in starters or f"{canonical}_async" in starters, (
            f"{tool} is paid but has no async form: a cancelled or disconnected "
            "call would lose the spend with no way to recover the result."
        )


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("claude_consult_async", {"prompt": "is this plan sound?"}),
        ("claude_adversarial_review_async", {"target": "ship on Friday"}),
    ],
)
async def test_paid_async_lifecycle_returns_the_blocking_tools_envelope(
    monkeypatch, git_repo, tmp_path, tool, args
):
    """Launch -> poll -> result, end to end through the MCP surface.

    The point of the fetched envelope's `tool` field: it names the tool whose
    contract the result honors (claude_consult / claude_adversarial_review), not
    the *_async starter, so a caller can hand the result to the same parser it
    uses for the blocking form.
    """
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", _fake_envelope(_INNER)], []),
    )
    expected_tool = tool.removesuffix("_async")
    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(tool, {**args, "workspace_root": str(git_repo)})
        )
        assert started["ok"] is True
        assert started["status"] == "running"
        # The handle names the job's own kind, not the starter: that is the tool
        # whose envelope claude_job_result will return.
        assert started["kind"] == expected_tool
        assert started["poll_after_ms"] > 0
        assert started["ttl_seconds"] > 0

        job_id = started["job_id"]
        assert (await _drain(client, job_id, str(git_repo)))["status"] == "done"

        res = structured(
            await client.call_tool(
                "claude_job_result", {"job_id": job_id, "workspace_root": str(git_repo)}
            )
        )
    assert res["ok"] is True
    assert res["tool"] == expected_tool
    assert res["verdict"] == "concerns"
    assert res["meta"]["job_id"] == job_id


async def test_consult_async_asks_the_question_not_a_diff_review(monkeypatch, git_repo, tmp_path):
    """The starter must build claude_consult's prompt, not the review lead-in.

    build_prompt is keyed by tool name and shared with the blocking path, so a
    starter that passed the wrong name would quietly ask Claude to review a diff
    that is not attached. build_command receives the finished prompt, so capturing
    there reads exactly what streams to the worker's stdin.
    """
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    seen = _capture_prompts(monkeypatch)
    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_consult_async",
                {
                    "prompt": "should we shard by tenant?",
                    "context": "10k tenants",
                    "workspace_root": str(git_repo),
                },
            )
        )
        await _drain(client, started["job_id"], str(git_repo))
    assert len(seen) == 1
    assert "independent second opinion" in seen[0]
    assert "should we shard by tenant?" in seen[0]
    assert "10k tenants" in seen[0]
    assert "Review the following code changes" not in seen[0]


async def test_adversarial_async_attaches_the_diff(monkeypatch, git_repo, tmp_path):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    seen = _capture_prompts(monkeypatch)
    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_adversarial_review_async",
                {
                    "target": "the subtraction is intentional",
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                },
            )
        )
        await _drain(client, started["job_id"], str(git_repo))
    assert "the subtraction is intentional" in seen[0]
    assert "Related changes" in seen[0]
    assert "a - b" in seen[0]  # the gathered diff really was attached


async def test_adversarial_async_empty_diff_skips_job_start(monkeypatch, git_repo, tmp_path):
    """An empty attached diff costs nothing and starts no job.

    Like claude_review_changes_async, the launch answers with a SuccessResult
    rather than a job handle here. That third success shape is the known wart
    issue #80 tracks; this pins the no-spend behavior so a fix there cannot
    quietly start charging for empty diffs.
    """
    import subprocess as _sp

    _sp.run(["git", "checkout", "--", "app.py"], cwd=git_repo, check=True)
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        claude_mod,
        "build_command",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("job should not start")),
    )
    async with Client(mcp) as client:
        data = structured(
            await client.call_tool(
                "claude_adversarial_review_async",
                {
                    "target": "nothing changed",
                    "scope": "working_tree",
                    "workspace_root": str(git_repo),
                },
            )
        )
    assert data["ok"] is True
    assert data["tool"] == "claude_adversarial_review"
    assert data["verdict"] == "unknown"
    assert "job_id" not in data


async def test_consult_async_cannot_answer_with_a_result():
    """claude_consult_async has no diff to find empty, so it never returns a
    SuccessResult — and its advertised schema must not claim otherwise.

    This is not only discovery-cost hygiene: a starter that advertises a branch
    it cannot produce makes the caller write a dead branch and weakens the
    remaining ones."""
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

    def branches(name):
        return tools[name].outputSchema["anyOf"]

    def has_result_branch(name):
        # `verdict` appears only on the SuccessResult branch, and _slim strips the
        # pydantic model titles, so the field is the durable discriminator.
        return any("verdict" in b.get("properties", {}) for b in branches(name))

    assert not has_result_branch("claude_consult_async")
    assert has_result_branch("claude_review_changes_async"), (
        "the diff-bearing starter still needs the empty-diff branch"
    )
    assert len(branches("claude_consult_async")) == len(branches("claude_review_changes_async")) - 1


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("claude_consult_async", {"prompt": "sound?"}),
        ("claude_adversarial_review_async", {"target": "ship it"}),
    ],
)
async def test_async_starters_replay_a_matching_idempotency_key(monkeypatch, git_repo, tool, args):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    call = {**args, "workspace_root": str(git_repo), "idempotency_key": "k-1"}
    async with Client(mcp) as client:
        first = structured(await client.call_tool(tool, call))
        second = structured(await client.call_tool(tool, call))
        await client.call_tool(
            "claude_job_cancel", {"job_id": first["job_id"], "workspace_root": str(git_repo)}
        )
    assert first["ok"] is True
    assert second["job_id"] == first["job_id"]
    assert second["status"] == "running"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("claude_consult_async", {"prompt": "a"}),
        ("claude_adversarial_review_async", {"target": "a"}),
    ],
)
async def test_async_starters_conflict_on_a_reused_key_with_new_arguments(
    monkeypatch, git_repo, tool, args
):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    field = next(iter(args))
    base = {**args, "workspace_root": str(git_repo), "idempotency_key": "k-2"}
    async with Client(mcp) as client:
        first = structured(await client.call_tool(tool, base))
        clash = structured(
            await client.call_tool(tool, {**base, field: "something else"}, raise_on_error=False)
        )
        await client.call_tool(
            "claude_job_cancel", {"job_id": first["job_id"], "workspace_root": str(git_repo)}
        )
    assert clash["ok"] is False
    assert clash["error"]["code"] == "idempotency_conflict"


async def test_consult_async_caps_free_form_input_before_spending(monkeypatch, git_repo):
    """The size cap runs before the job starts, like the blocking form's."""
    # max_input_bytes() floors at 1_000, so that floor is the cap under test.
    monkeypatch.setenv("CLAUDE_IN_CODEX_MAX_INPUT_BYTES", "1000")
    monkeypatch.setattr(
        claude_mod,
        "build_command",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("job should not start")),
    )
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_consult_async",
                {"prompt": "x" * 5_000, "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
    assert out["ok"] is False
    assert out["error"]["code"] == "context_too_large"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"paths": ["app.py"]}, "invalid_paths"),
        ({"head": "HEAD"}, "invalid_head"),
    ],
)
async def test_adversarial_async_rejects_diff_arguments_without_scope(git_repo, args, code):
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_adversarial_review_async",
                {"target": "t", "workspace_root": str(git_repo), **args},
                raise_on_error=False,
            )
        )
    assert out["ok"] is False
    assert out["error"]["code"] == code
    # The repair names the async tool the caller actually invoked.
    assert "claude_adversarial_review_async" in json.dumps(out["error"])


_NEW_STARTERS = [
    ("claude_consult_async", {"prompt": "x"}),
    ("claude_adversarial_review_async", {"target": "x"}),
]


@pytest.mark.parametrize(("tool", "args"), _NEW_STARTERS)
async def test_async_starters_report_a_bad_env_config_mode(monkeypatch, git_repo, tool, args):
    """The preflight config error fires before any job is started, and so before
    any spend — the same ordering the blocking tools have."""
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "bogus")
    monkeypatch.setattr(
        claude_mod,
        "build_command",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("job should not start")),
    )
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                tool, {**args, "workspace_root": str(git_repo)}, raise_on_error=False
            )
        )
    assert out["ok"] is False
    assert out["error"]["code"] == "unsupported_config_mode"


@pytest.mark.parametrize(("tool", "args"), _NEW_STARTERS)
async def test_async_starters_refuse_an_unverifiable_legacy_key(monkeypatch, git_repo, tool, args):
    """A 0.7 marker records no argument digest, so replaying it could hand back a
    paid answer to a question the caller did not ask. Every starter must refuse
    it, not only the one that existed when the markers were written."""
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    cwd = str(git_repo)
    cfg = jobs_mod.JobConfig(
        kind="claude_consult",
        config_mode="inherit",
        access="toolless",
        scope=None,
        base=None,
        head=None,
        detail="summary",
        timeout_seconds=1800,
        workspace_source="cwd",
        context_summary=None,
    )
    job_id, _ = jobs_mod.start_job(["sh", "-c", "sleep 30"], cwd, cfg)
    marker = jobs_mod._reservation_path(cwd, "legacy-key")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"job_id": job_id, "created_epoch": time.time()}))
    try:
        async with Client(mcp) as client:
            out = structured(
                await client.call_tool(
                    tool,
                    {**args, "workspace_root": cwd, "idempotency_key": "legacy-key"},
                    raise_on_error=False,
                )
            )
    finally:
        jobs_mod.cancel(cwd, job_id)
    assert out["ok"] is False
    assert out["error"]["code"] == "idempotency_conflict"
    assert out["error"]["action"]["tool"] == "claude_job_status"
    assert out["error"]["action"]["arguments"]["job_id"] == job_id


async def test_adversarial_async_rejects_an_escaping_path(git_repo):
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_adversarial_review_async",
                {
                    "target": "x",
                    "scope": "working_tree",
                    "paths": ["../secret"],
                    "workspace_root": str(git_repo),
                },
                raise_on_error=False,
            )
        )
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_paths"
    assert out["error"]["details"]["field"] == "paths"


async def test_adversarial_async_caps_free_form_input_before_spending(monkeypatch, git_repo):
    # max_input_bytes() floors at 1_000, so that floor is the cap under test.
    monkeypatch.setenv("CLAUDE_IN_CODEX_MAX_INPUT_BYTES", "1000")
    monkeypatch.setattr(
        claude_mod,
        "build_command",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("job should not start")),
    )
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_adversarial_review_async",
                {"target": "x" * 5_000, "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
    assert out["ok"] is False
    assert out["error"]["code"] == "context_too_large"


async def test_a_keyed_retry_that_changes_only_detail_replays(monkeypatch, git_repo, tmp_path):
    """`detail` is deliberately NOT an effective argument, and this pins that.

    It selects how a stored result is rendered, not what Claude is asked or paid
    to do, and the record keeps the raw envelope — so the replayed job can still
    be read at full density for free. Treating `detail` as effective would make
    this an idempotency_conflict and push the caller into a SECOND PAID RUN to
    obtain a rendering that was already free, which is the opposite of what the
    key exists for. The second half of this test is the part that earns the
    exclusion: it shows the full rendering really is recoverable.
    """
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    inner = {
        "summary": "S" * 400,
        "verdict": "pass",
        "confidence": "high",
        "findings": [],
        "questions": [],
        "assumptions": [],
    }
    monkeypatch.setattr(
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", _fake_envelope(inner)], []),
    )
    base = {"prompt": "q", "workspace_root": str(git_repo), "idempotency_key": "detail-key"}
    async with Client(mcp) as client:
        first = structured(
            await client.call_tool("claude_consult_async", {**base, "detail": "summary"})
        )
        second = structured(
            await client.call_tool(
                "claude_consult_async", {**base, "detail": "full"}, raise_on_error=False
            )
        )
        assert first["ok"] is True
        assert second["ok"] is True, "changing only detail must replay, not conflict"
        assert second["job_id"] == first["job_id"]

        job_id = first["job_id"]
        assert (await _drain(client, job_id, str(git_repo)))["status"] == "done"
        stored = structured(
            await client.call_tool(
                "claude_job_result", {"job_id": job_id, "workspace_root": str(git_repo)}
            )
        )
        rerendered = structured(
            await client.call_tool(
                "claude_job_result",
                {"job_id": job_id, "workspace_root": str(git_repo), "detail": "full"},
            )
        )
    # The job kept the level it was STARTED with...
    assert stored["raw_response"].get("text") is None
    # ...and the density the replayed call asked for is still available, free.
    assert rerendered["raw_response"]["text"] is not None
    assert len(rerendered["summary"]) == 400


async def test_capability_summary_does_not_promise_an_alias_async_form():
    """CAPABILITY_SUMMARY is first-read instruction text, so a universal claim in
    it sends agents to tools that do not exist. `paid_tools` includes the
    deprecated `claude_ask`, and there is no `claude_ask_async`."""
    data = _capabilities_payload()
    starters = set(data["async_lifecycle"]["start_tools"])
    blocking = [t for t in data["paid_tools"] if t not in starters]
    # The deprecated alias is the ONE blocking paid tool with no async form, and
    # it is deprecated rather than missing one. Any other name reaching this list
    # means a new paid tool shipped blocking-only.
    assert [t for t in blocking if f"{t}_async" not in starters] == ["claude_ask"]
    summary = CAPABILITY_SUMMARY.lower()
    assert "deprecated aliases do not" in summary
    # And it must not tell a caller to assume the handle it may not get.
    assert "absent on an empty diff" in summary


async def test_one_key_cannot_replay_across_two_starters(monkeypatch, git_repo):
    """The idempotency index keys on (namespace, key), and all three starters
    share one namespace — so a key is unique per WORKSPACE, not per tool.

    `jobs._IDEMPOTENCY_NAMESPACE` asserts in a comment that reusing a key across
    two starters conflicts rather than replaying the first tool's job. Nothing
    tested it, and the digest itself carries only (argv, prompt), not the job
    kind. A cross-tool replay would be the worst failure this key has: it would
    hand back a paid answer to a question the caller never asked.
    """
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(git_repo / ".state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    ws = str(git_repo)
    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_consult_async",
                {"prompt": "q", "workspace_root": ws, "idempotency_key": "shared"},
            )
        )
        crossed = structured(
            await client.call_tool(
                "claude_adversarial_review_async",
                {"target": "t", "workspace_root": ws, "idempotency_key": "shared"},
                raise_on_error=False,
            )
        )
        await client.call_tool(
            "claude_job_cancel", {"job_id": started["job_id"], "workspace_root": ws}
        )
    assert started["ok"] is True
    assert crossed["ok"] is False
    assert crossed["error"]["code"] == "idempotency_conflict"
    # The decisive part: the other tool's job was never handed over.
    assert crossed.get("job_id") != started["job_id"]


async def test_an_unwritable_state_dir_is_not_reported_as_a_missing_cli(
    monkeypatch, git_repo, tmp_path
):
    """The two OSError sources a launch has need opposite repairs.

    Matching the bare FileNotFoundError/PermissionError types conflated them, so
    an unwritable job-state directory answered "Install Claude Code and ensure
    `claude` is on PATH" — a wrong diagnosis pointing at the wrong fix, for a CLI
    that was installed and working.
    """
    state = tmp_path / "locked"
    state.mkdir()
    state.chmod(0o500)  # readable, not writable
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(state))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: (["sh", "-c", "sleep 30"], []))
    try:
        async with Client(mcp) as client:
            out = structured(
                await client.call_tool(
                    "claude_consult_async",
                    {"prompt": "q", "workspace_root": str(git_repo)},
                    raise_on_error=False,
                )
            )
    finally:
        state.chmod(0o700)
    assert out["ok"] is False
    assert out["error"]["code"] == "internal_error"
    assert "claude" not in out["error"]["repair"].lower()
    assert "director" in out["error"]["repair"].lower()


async def test_a_non_executable_claude_still_reports_claude_not_found(
    monkeypatch, git_repo, tmp_path
):
    """The other side of the split: a present-but-unrunnable CLI must not be
    reported as a state-directory problem either. The repair names chmod, not
    install, because the binary is already there."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o600)  # present, not executable
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(claude_mod, "build_command", lambda *a, **k: ([str(fake)], []))
    async with Client(mcp) as client:
        out = structured(
            await client.call_tool(
                "claude_consult_async",
                {"prompt": "q", "workspace_root": str(git_repo)},
                raise_on_error=False,
            )
        )
    assert out["ok"] is False
    assert out["error"]["code"] == "claude_not_found"
    assert "not executable" in out["error"]["message"]
    assert "chmod" in out["error"]["repair"]


async def test_job_lifecycle_prose_is_not_review_only():
    """The lifecycle now serves three starters, so its own tools must not describe
    only diff review. Clients that read tool descriptions rather than calling
    claude_capabilities were otherwise never told the new starters use it."""
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in (
        "claude_job_status",
        "claude_job_result",
        "claude_job_consume_result",
        "claude_job_cancel",
        "claude_job_list",
    ):
        text = tools[name].description
        assert "review job" not in text, name
        assert "background review" not in text, name
        assert "claude_review_changes_async" not in text, name
    # And the result tool must not promise one specific tool's envelope.
    assert "`kind`" in tools["claude_job_result"].description
    assert "claude_review_changes envelope" not in tools["claude_job_result"].description
