"""FastMCP server exposing Claude Code as bounded, read-only critique tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Annotated, Literal, cast
from urllib.parse import unquote, urlparse

from anyio.to_thread import run_sync
from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from cc_plugin_codex import __version__, cli_contract, jobs, preflight
from cc_plugin_codex.claude import (
    auth_status,
    build_command,
    classify_failure,
    run_claude_async,
)
from cc_plugin_codex.config import (
    MAX_BUDGET_USD,
    MAX_TIMEOUT_SECONDS,
    MIN_BUDGET_USD,
    MIN_TIMEOUT_SECONDS,
    VALID_EFFORTS,
    bare_available,
    clamp_budget,
    clamp_timeout,
    defaults,
    hook_security_warnings,
    hooks_disabled_available,
    max_input_bytes,
    safe_available,
    sanitize_effort,
    supported_majors,
    version_supported,
    workspace_hook_settings,
)
from cc_plugin_codex.context import (
    MAX_DIFF_BYTES,
    InvalidBaseError,
    InvalidPathsError,
    InvalidScopeError,
    gather_context,
    normalize_paths,
)
from cc_plugin_codex.jobs import JobConfig
from cc_plugin_codex.normalize import apply_cost_usage, build_prompt, normalize_envelope
from cc_plugin_codex.schemas import (
    CAPABILITIES_SCHEMA,
    DRY_RUN_SCHEMA,
    FINGERPRINT,
    JOB_LIST_SCHEMA,
    JOB_STARTED_SCHEMA,
    JOB_STATUS_SCHEMA,
    RESULT_SCHEMA,
    STATUS_SCHEMA,
    Access,
    CapabilitiesResult,
    Confidence,
    ConfigMode,
    Detail,
    DryRunResult,
    Effort,
    ErrorCode,
    ErrorInfo,
    ErrorResult,
    JobStarted,
    Meta,
    RawDefaults,
    RawResponse,
    ResolvedDefaults,
    Scope,
    StatusResult,
    SuccessResult,
    ToolCapability,
    Verdict,
    workspace_warning_for,
)

CAPABILITY_SUMMARY = (
    "cc-plugin-codex lets Codex ask Claude Code for bounded critique: diff reviews, "
    "adversarial plan review, and second opinions. It never edits code, grants "
    "Bash/write tools, or proxies Claude MCP tools; Claude Code hooks may still run "
    "unless config_mode=safe or bare is used. Paid tools send context to Anthropic; call "
    "claude_status before spending. claude_review_changes blocks; "
    "claude_review_changes_async runs in background with poll/result/cancel; "
    "claude_review_dry_run previews workspace/diff-size/redaction for free. Findings "
    "are advisory claims to verify. Pass workspace_root explicitly: it defaults to "
    "the first MCP root, else server cwd; when roots exist it must be inside one. "
    "access=toolless is default; access=readonly lets Claude read files directly, "
    "bypassing server-gathered diff redaction. Free-form input is capped by "
    "CC_PLUGIN_CODEX_MAX_INPUT_BYTES. Experimental; pin fingerprint."
)

PRACTICAL_MIN_BUDGET_HINT = (
    "The configured clamp allows $0.01+, but real paid calls usually need about "
    "$0.10-$0.20 even for small prompts; lower budgets may spend and still return "
    "budget_exceeded."
)

mcp = FastMCP(name="cc-plugin-codex", instructions=CAPABILITY_SUMMARY)

# Paid tools read code but are NOT idempotent (each call spends money and re-invokes
# Claude) and are explicitly non-destructive (no writes/shell). openWorld: they reach
# an external service (Anthropic).
_PAID_ANNOTATIONS = {
    "readOnlyHint": True,
    "openWorldHint": True,
    "destructiveHint": False,
    "idempotentHint": False,
}
# Free read-only tools are safely repeatable.
_FREE_READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
}
# Local job lifecycle mutations change only this server's job state.
_LOCAL_MUTATION_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
}


def _result(payload: dict) -> ToolResult:
    """Wrap a normalized payload as a ToolResult, flagging error envelopes.

    Keeps the structured ok:true|false contract intact AND sets the native
    is_error flag for ok:false, so clients that branch on is_error (not just the
    `ok` field) detect failures.
    """
    return ToolResult(structured_content=payload, is_error=payload.get("ok") is False)


def _meta(
    cwd: str,
    config_mode: str,
    access: str,
    timeout: int,
    elapsed: int,
    exit_code: int | None,
    scope: str | None = None,
    base: str | None = None,
    paths: list[str] | None = None,
    truncated: bool = False,
    hint: str | None = None,
    workspace_source: str | None = None,
    requested_budget: float | None = None,
    redacted_paths: list[str] | None = None,
    compat_warnings: list[str] | None = None,
    security_warnings: list[str] | None = None,
) -> Meta:
    return Meta(
        cwd=cwd,
        config_mode=cast("ConfigMode", config_mode),
        access=cast("Access", access),
        scope=scope,
        base=base,
        paths=paths,
        timeout_seconds=timeout,
        elapsed_ms=elapsed,
        command_exit_code=exit_code,
        truncated=truncated,
        truncation_hint=hint,
        fingerprint=FINGERPRINT,
        workspace_source=workspace_source,
        workspace_warning=workspace_warning_for(workspace_source, cwd),
        requested_max_budget_usd=requested_budget,
        redacted_paths=redacted_paths or [],
        compat_warnings=compat_warnings or [],
        security_warnings=security_warnings or [],
    )


def _err(
    code: str,
    message: str,
    repair: str,
    meta: Meta,
    offending: str | None = None,
    retryable: bool = False,
) -> dict:
    return ErrorResult(
        error=ErrorInfo(
            code=cast("ErrorCode", code),
            message=message,
            repair=repair,
            offending_param=offending,
            retryable=retryable,
        ),
        meta=meta,
    ).model_dump(mode="json", exclude_none=True)


def _invalid_paths_error(meta: Meta, message: str | None = None) -> dict:
    return _err(
        "invalid_paths",
        message or "Invalid paths filter.",
        "Pass plain repo-relative paths such as paths=['src', 'tests/test_context.py']; "
        "omit paths or pass [] for an unfiltered diff.",
        meta,
        offending="paths",
    )


def _resolve_paths(paths: list[str] | None, meta: Meta) -> tuple[list[str] | None, dict | None]:
    try:
        return normalize_paths(paths), None
    except InvalidPathsError as exc:
        return None, _invalid_paths_error(meta, str(exc))


def _workspace_error(code: str, workspace_root: str | None = None) -> dict:
    meta = _meta("", "inherit", "toolless", 0, 0, None)
    if code == "workspace_outside_roots":
        return _err(
            code,
            f"workspace_root '{workspace_root}' is outside the client's MCP roots.",
            "Pass a workspace_root contained by an MCP root, omit workspace_root to "
            "use the first root, or configure the intended directory as a root.",
            meta,
            offending="workspace_root",
        )
    if workspace_root is None:
        return _err(
            code,
            "The resolved workspace is not an existing absolute directory.",
            "Pass workspace_root as an absolute path to an existing directory, "
            "or configure an MCP root that points at an existing directory.",
            meta,
        )
    return _err(
        code,
        f"workspace_root '{workspace_root}' is not an existing absolute directory.",
        "Pass workspace_root as an absolute path to an existing directory, or "
        "configure an MCP root.",
        meta,
        offending="workspace_root",
    )


async def _file_roots(ctx) -> list[str]:
    """Return filesystem paths from the client's file:// roots.

    Returns [] if the client provides no roots or does not support the roots
    capability (list_roots raises)."""
    if ctx is None:
        return []
    try:
        roots = await ctx.list_roots()
    except Exception:
        return []
    paths = []
    for root in roots or []:
        uri = str(getattr(root, "uri", ""))
        if uri.startswith("file://"):
            paths.append(unquote(urlparse(uri).path))
    return paths


async def _first_root(ctx) -> str | None:
    roots = await _file_roots(ctx)
    return roots[0] if roots else None


def _contained_by(path: str, root: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.realpath(path), os.path.realpath(root)]
        ) == os.path.realpath(root)
    except ValueError:
        return False


async def _resolve_workspace(workspace_root, ctx):
    """Resolve the workspace directory.

    Order: explicit workspace_root arg -> first file:// MCP root -> os.getcwd().
    Returns (path, error_code, source). error_code is None on success; on failure
    path is None and source is None."""
    roots = await _file_roots(ctx)
    if workspace_root:
        path, source = workspace_root, "param"
    else:
        root = roots[0] if roots else None
        if root:
            path, source = root, "roots"
        else:
            path, source = os.getcwd(), "cwd"  # noqa: PTH109 — path stays a str (returned as cwd)
    # An explicit workspace_root must be absolute: a relative path would be resolved
    # against the very cwd this resolution exists to stop trusting. Roots (file:// URIs)
    # and os.getcwd() are always absolute already.
    if not os.path.isabs(path) or not os.path.isdir(path):  # noqa: PTH117, PTH112 — path is a str by contract
        return None, "invalid_workspace_root", None
    if workspace_root and roots and not any(_contained_by(path, root) for root in roots):
        return None, "workspace_outside_roots", None
    return path, None, source


def _utf8_len(value: str | None) -> int:
    return len((value or "").encode("utf-8", "replace"))


def _validate_input_size(fields: dict[str, str | None], meta: Meta) -> dict | None:
    limit = max_input_bytes()
    total = sum(_utf8_len(value) for value in fields.values())
    if total <= limit:
        return None
    largest = max(fields, key=lambda key: _utf8_len(fields[key]))
    return _err(
        "context_too_large",
        f"User-supplied text is {total} bytes, exceeding the {limit}-byte limit.",
        "Shorten the prompt/evidence/context, split the request, or raise "
        "CC_PLUGIN_CODEX_MAX_INPUT_BYTES if this workspace intentionally allows it.",
        meta,
        offending=largest,
    )


def _empty_diff_result(
    tool: str,
    meta: Meta,
    context_summary,
    paths: list[str] | None = None,
    verdict: Verdict = "pass",
    confidence: Confidence = "high",
) -> dict:
    summary = "No changes in scope; skipped Claude call."
    if paths:
        summary = "No changes matched paths; skipped Claude call."
    result = SuccessResult(
        tool=tool,
        summary=summary,
        verdict=verdict,
        confidence=confidence,
        raw_response=RawResponse(),
        context_summary=context_summary,
        meta=meta,
    )
    return result.model_dump(mode="json", exclude_none=True)


@dataclass
class Resolved:
    config_mode: str
    access: str
    model: str | None
    budget: float
    timeout: int
    detail: str
    effort: str


def _resolve(
    config_mode,
    access,
    model,
    max_budget_usd,
    timeout_seconds,
    detail,
    cwd,
    scope=None,
    base=None,
    paths: list[str] | None = None,
    workspace_source=None,
    effort=None,
):
    """Resolve env defaults + clamps and validate.

    Returns (Resolved, None) or (None, error_dict).
    """
    d = defaults()
    cm = config_mode or d.config_mode
    ac = access or d.access
    mdl = model or d.model
    budget = clamp_budget(max_budget_usd if max_budget_usd is not None else d.max_budget_usd)
    timeout = clamp_timeout(timeout_seconds if timeout_seconds is not None else d.timeout_seconds)
    det = detail if detail in ("summary", "full") else "summary"
    eff = effort if effort in VALID_EFFORTS else d.effort

    # Validate before building Meta (Meta uses Literal types — invalid values
    # would raise Pydantic errors before we can return a structured response).
    if cm not in ("inherit", "scoped", "safe", "bare"):
        safe_meta = _meta(
            cwd,
            "inherit",
            ac if ac in ("toolless", "readonly") else "toolless",
            timeout,
            0,
            None,
            scope,
            base,
            paths,
            workspace_source=workspace_source,
            requested_budget=budget,
        )
        return None, _err(
            "unsupported_config_mode",
            f"Unknown config_mode '{cm}'.",
            "Use one of: inherit, scoped, safe, bare.",
            safe_meta,
            offending="config_mode",
        )
    if ac not in ("toolless", "readonly"):
        safe_meta = _meta(
            cwd,
            cm,
            "toolless",
            timeout,
            0,
            None,
            scope,
            base,
            paths,
            workspace_source=workspace_source,
            requested_budget=budget,
        )
        return None, _err(
            "unsupported_access",
            f"Unknown access '{ac}'.",
            "Use one of: toolless, readonly.",
            safe_meta,
            offending="access",
        )

    if cm == "safe":
        fs = preflight.flag_support()
        if not safe_available(fs.help_parsed, fs.supported):
            safe_meta = _meta(
                cwd,
                "safe",
                ac,
                timeout,
                0,
                None,
                scope,
                base,
                paths,
                workspace_source=workspace_source,
                requested_budget=budget,
            )
            return None, _err(
                "unsupported_config_mode",
                "config_mode=safe requires a Claude CLI with --safe-mode support.",
                "Update Claude Code, or use config_mode inherit/scoped/bare.",
                safe_meta,
                offending="config_mode",
            )

    meta = _meta(
        cwd,
        cm,
        ac,
        timeout,
        0,
        None,
        scope,
        base,
        paths,
        workspace_source=workspace_source,
        requested_budget=budget,
        security_warnings=hook_security_warnings(cwd, cm),
    )
    if cm == "bare" and not bare_available():
        return None, _err(
            "api_key_missing",
            "config_mode=bare requires ANTHROPIC_API_KEY, which is unset.",
            "Set ANTHROPIC_API_KEY, or use config_mode inherit/scoped/safe.",
            meta,
            offending="config_mode",
        )
    return Resolved(cm, ac, mdl, budget, timeout, det, eff), None


def _resolve_config_mode_only(
    config_mode: str | None,
    cwd: str,
    scope: str | None = None,
    base: str | None = None,
    paths: list[str] | None = None,
    workspace_source: str | None = None,
) -> tuple[str | None, dict | None]:
    d = defaults()
    cm = config_mode or d.config_mode
    meta = _meta(
        cwd,
        cm if cm in ("inherit", "scoped", "safe", "bare") else "inherit",
        "toolless",
        0,
        0,
        None,
        scope,
        base,
        paths,
        workspace_source=workspace_source,
    )
    if cm not in ("inherit", "scoped", "safe", "bare"):
        return None, _err(
            "unsupported_config_mode",
            f"Unknown config_mode '{cm}'.",
            "Use one of: inherit, scoped, safe, bare.",
            meta,
            offending="config_mode",
        )
    if cm == "safe":
        fs = preflight.flag_support()
        if not safe_available(fs.help_parsed, fs.supported):
            return None, _err(
                "unsupported_config_mode",
                "config_mode=safe requires a Claude CLI with --safe-mode support.",
                "Update Claude Code, or use config_mode inherit/scoped/bare.",
                meta,
                offending="config_mode",
            )
    return cm, None


async def _execute(
    tool,
    payload,
    r: Resolved,
    cwd,
    scope=None,
    base=None,
    paths: list[str] | None = None,
    context_text="",
    context_summary=None,
    workspace_source=None,
    redacted_paths: list[str] | None = None,
) -> dict:
    prompt = build_prompt(tool, payload, context_text)
    cmd, dropped = build_command(prompt, r.config_mode, r.access, r.model, r.budget, r.effort)
    run = await run_claude_async(cmd, cwd=cwd, timeout_seconds=r.timeout, stdin_text=prompt)
    meta = _meta(
        cwd,
        r.config_mode,
        r.access,
        r.timeout,
        run.elapsed_ms,
        run.exit_code,
        scope,
        base,
        paths,
        workspace_source=workspace_source,
        requested_budget=r.budget,
        redacted_paths=redacted_paths,
        compat_warnings=dropped,
        security_warnings=hook_security_warnings(cwd, r.config_mode),
    )
    if run.exit_code != 0 or run.timed_out:
        # A non-zero exit can still carry a cost-bearing JSON envelope (e.g.
        # budget_exceeded); report what it spent when available.
        try:
            env = json.loads(run.stdout)
        except (json.JSONDecodeError, ValueError, TypeError):
            env = None
        if isinstance(env, dict):
            apply_cost_usage(meta, env)
        info = classify_failure(run)
        return _err(info.code, info.message, info.repair, meta, retryable=info.retryable)
    return normalize_envelope(
        tool, run.stdout, meta, detail=r.detail, context_summary=context_summary
    )


@mcp.tool(
    annotations=_PAID_ANNOTATIONS, title="Ask Claude (second opinion)", output_schema=RESULT_SCHEMA
)
async def claude_ask(
    prompt: Annotated[str, Field(description="The question to ask Claude.")],
    context: Annotated[str | None, Field(description="Extra context, passed verbatim.")] = None,
    workspace_root: Annotated[
        str | None,
        Field(
            description="Absolute path to the repo/workspace to operate in. If omitted, "
            "the server uses the client's first MCP root, else its own cwd."
        ),
    ] = None,
    config_mode: Annotated[ConfigMode | None, Field(description="inherit|scoped|safe|bare")] = None,
    access: Annotated[Access | None, Field(description="toolless|readonly")] = None,
    model: Annotated[
        str | None, Field(description="Claude model override; omit for configured default.")
    ] = None,
    effort: Annotated[
        Effort | None,
        Field(
            description="Reasoning effort: low|medium|high|xhigh|max. "
            "Raise for high-stakes reviews; omit to use the server default."
        ),
    ] = None,
    max_budget_usd: Annotated[
        float | None, Field(description="Per-call Claude spend cap; clamped by server limits.")
    ] = None,
    timeout_seconds: Annotated[
        int | None, Field(description="Sync call timeout; omit for configured default.")
    ] = None,
    detail: Annotated[Detail, Field(description="summary|full")] = "summary",
    ctx: Context | None = None,
) -> ToolResult:
    """Ask Claude for a free-form second opinion.

    Use when the task is a question or design choice, not a git diff review or
    adversarial attack. Paid external call; read-only; blocks up to
    timeout_seconds and can be cancelled but not resumed. Free-form input is
    size-capped before spend. Returns structured ok:true findings or ok:false
    repair errors.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    r, err = _resolve(
        config_mode,
        access,
        model,
        max_budget_usd,
        timeout_seconds,
        detail,
        cwd,
        workspace_source=ws_source,
        effort=effort,
    )
    if err:
        return _result(err)
    payload = {"prompt": prompt, "context": context}
    meta = _meta(
        cwd,
        r.config_mode,
        r.access,
        r.timeout,
        0,
        None,
        workspace_source=ws_source,
        requested_budget=r.budget,
    )
    too_large = _validate_input_size(payload, meta)
    if too_large:
        return _result(too_large)
    out = await _execute("claude_ask", payload, r, cwd, workspace_source=ws_source)
    return _result(out)


@mcp.tool(
    annotations=_PAID_ANNOTATIONS, title="Review changes with Claude", output_schema=RESULT_SCHEMA
)
async def claude_review_changes(
    scope: Annotated[Scope, Field(description="working_tree|staged|branch")],
    base: Annotated[str, Field(description="Base ref for scope=branch.")] = "main",
    focus: Annotated[str | None, Field(description="e.g. 'security', 'tests'.")] = None,
    paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional plain repo-relative paths to filter the server-provided diff. "
                "No pathspec magic or excludes; []/omitted means unfiltered."
            )
        ),
    ] = None,
    workspace_root: Annotated[
        str | None,
        Field(
            description="Absolute path to the repo/workspace to operate in. If omitted, "
            "the server uses the client's first MCP root, else its own cwd."
        ),
    ] = None,
    config_mode: Annotated[ConfigMode | None, Field(description="inherit|scoped|safe|bare")] = None,
    access: Annotated[Access | None, Field(description="toolless|readonly")] = None,
    model: Annotated[
        str | None, Field(description="Claude model override; omit for configured default.")
    ] = None,
    effort: Annotated[
        Effort | None,
        Field(
            description="Reasoning effort: low|medium|high|xhigh|max. "
            "Raise for high-stakes reviews; omit to use the server default."
        ),
    ] = None,
    max_budget_usd: Annotated[
        float | None, Field(description="Per-call Claude spend cap; clamped by server limits.")
    ] = None,
    timeout_seconds: Annotated[
        int | None, Field(description="Sync call timeout; omit for configured default.")
    ] = None,
    detail: Annotated[Detail, Field(description="summary|full")] = "summary",
    ctx: Context | None = None,
) -> ToolResult:
    """Review a git diff with Claude and wait for the result.

    Use for correctness, regression, security, or test-coverage review of
    working_tree, staged, or branch diff. Paid external call; read-only; blocks up
    to timeout_seconds and can be cancelled but not resumed. For long reviews, use
    claude_review_changes_async. Empty diffs return ok:true without calling Claude.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    # Validate options BEFORE touching git, so bad config isn't masked by git errors.
    r, err = _resolve(
        config_mode,
        access,
        model,
        max_budget_usd,
        timeout_seconds,
        detail,
        cwd,
        scope=scope,
        base=base,
        paths=paths,
        workspace_source=ws_source,
        effort=effort,
    )
    if err:
        return _result(err)
    meta = _meta(
        cwd,
        r.config_mode,
        r.access,
        r.timeout,
        0,
        None,
        scope,
        base,
        paths,
        workspace_source=ws_source,
        requested_budget=r.budget,
    )
    effective_paths, paths_err = _resolve_paths(paths, meta)
    if paths_err:
        return _result(paths_err)
    try:
        ctx_data = await run_sync(
            lambda: gather_context(cwd, scope=scope, base=base, paths=effective_paths)
        )
    except InvalidBaseError:
        return _result(
            _err(
                "invalid_base",
                f"Invalid base ref '{base}'.",
                "Use an existing git ref matching [A-Za-z0-9._/-]+ that does not start with '-'.",
                meta,
                offending="base",
            )
        )
    except InvalidScopeError:
        return _result(
            _err(
                "invalid_scope",
                f"Invalid scope '{scope}'.",
                "Use working_tree, staged, or branch.",
                meta,
                offending="scope",
            )
        )
    except RuntimeError as e:
        return _result(
            _err(
                "internal_error",
                f"git failed: {e}",
                "Ensure cwd is a git repo and base ref exists.",
                meta,
            )
        )
    if ctx_data.truncated:
        meta = _meta(
            cwd,
            r.config_mode,
            r.access,
            r.timeout,
            0,
            None,
            scope,
            base,
            effective_paths,
            truncated=True,
            hint=ctx_data.truncation_hint,
            workspace_source=ws_source,
            requested_budget=r.budget,
            redacted_paths=ctx_data.redacted_paths,
        )
        return _result(
            _err(
                "context_too_large",
                "The diff is too large to review safely.",
                ctx_data.truncation_hint or "Narrow the scope.",
                meta,
            )
        )
    meta = _meta(
        cwd,
        r.config_mode,
        r.access,
        r.timeout,
        0,
        None,
        scope,
        base,
        effective_paths,
        workspace_source=ws_source,
        requested_budget=r.budget,
        redacted_paths=ctx_data.redacted_paths,
        security_warnings=hook_security_warnings(cwd, r.config_mode),
    )
    if ctx_data.summary.files_changed == 0 and not ctx_data.text.strip():
        return _result(
            _empty_diff_result("claude_review_changes", meta, ctx_data.summary, effective_paths)
        )
    out = await _execute(
        "claude_review_changes",
        {"scope": scope, "base": base, "focus": focus, "paths": effective_paths},
        r,
        cwd,
        scope=scope,
        base=base,
        paths=effective_paths,
        context_text=ctx_data.text,
        context_summary=ctx_data.summary,
        workspace_source=ws_source,
        redacted_paths=ctx_data.redacted_paths,
    )
    return _result(out)


@mcp.tool(
    annotations=_PAID_ANNOTATIONS,
    title="Adversarial review with Claude",
    output_schema=RESULT_SCHEMA,
)
async def claude_adversarial_review(
    target: Annotated[str, Field(description="The plan/claim/decision to attack.")],
    evidence: Annotated[str | None, Field(description="Supporting evidence.")] = None,
    scope: Annotated[
        Scope | None, Field(description="Optionally attach a diff: working_tree|staged|branch")
    ] = None,
    base: Annotated[str, Field(description="Base ref for branch diff when scope=branch.")] = "main",
    paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional plain repo-relative paths for the attached server-provided diff. "
                "Requires scope; no pathspec magic or excludes."
            )
        ),
    ] = None,
    workspace_root: Annotated[
        str | None,
        Field(
            description="Absolute path to the repo/workspace to operate in. If omitted, "
            "the server uses the client's first MCP root, else its own cwd."
        ),
    ] = None,
    config_mode: Annotated[ConfigMode | None, Field(description="inherit|scoped|safe|bare")] = None,
    access: Annotated[Access | None, Field(description="toolless|readonly")] = None,
    model: Annotated[
        str | None, Field(description="Claude model override; omit for configured default.")
    ] = None,
    effort: Annotated[
        Effort | None,
        Field(
            description="Reasoning effort: low|medium|high|xhigh|max. "
            "Raise for high-stakes reviews; omit to use the server default."
        ),
    ] = None,
    max_budget_usd: Annotated[
        float | None, Field(description="Per-call Claude spend cap; clamped by server limits.")
    ] = None,
    timeout_seconds: Annotated[
        int | None, Field(description="Sync call timeout; omit for configured default.")
    ] = None,
    detail: Annotated[Detail, Field(description="summary|full")] = "summary",
    ctx: Context | None = None,
) -> ToolResult:
    """Have Claude attack a plan, claim, or decision.

    Use to surface counterarguments and failure modes. Include evidence text, and
    optionally attach a git diff with scope/base. Paid external call; read-only;
    blocks up to timeout_seconds and can be cancelled but not resumed. Free-form
    input is size-capped before spend; an empty attached diff returns ok:true
    without calling Claude.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    r, err = _resolve(
        config_mode,
        access,
        model,
        max_budget_usd,
        timeout_seconds,
        detail,
        cwd,
        scope=scope,
        base=base,
        paths=paths,
        workspace_source=ws_source,
        effort=effort,
    )
    if err:
        return _result(err)
    payload_text = {"target": target, "evidence": evidence}
    payload: dict[str, object] = dict(payload_text)
    meta = _meta(
        cwd,
        r.config_mode,
        r.access,
        r.timeout,
        0,
        None,
        scope,
        base,
        paths,
        workspace_source=ws_source,
        requested_budget=r.budget,
    )
    if paths and not scope:
        return _result(
            _invalid_paths_error(meta, "paths requires scope on claude_adversarial_review.")
        )
    too_large = _validate_input_size(payload_text, meta)
    if too_large:
        return _result(too_large)
    context_text = ""
    context_summary = None
    redacted_paths: list[str] = []
    effective_paths = None
    if scope:
        effective_paths, paths_err = _resolve_paths(paths, meta)
        if paths_err:
            return _result(paths_err)
        meta = _meta(
            cwd,
            r.config_mode,
            r.access,
            r.timeout,
            0,
            None,
            scope,
            base,
            effective_paths,
            workspace_source=ws_source,
            requested_budget=r.budget,
        )
        try:
            ctx_data = await run_sync(
                lambda: gather_context(cwd, scope=scope, base=base, paths=effective_paths)
            )
        except InvalidBaseError:
            return _result(
                _err(
                    "invalid_base",
                    f"Invalid base ref '{base}'.",
                    "Use an existing git ref matching [A-Za-z0-9._/-]+ that does "
                    "not start with '-'.",
                    meta,
                    offending="base",
                )
            )
        except InvalidScopeError:
            return _result(
                _err(
                    "invalid_scope",
                    f"Invalid scope '{scope}'.",
                    "Use working_tree, staged, or branch (or omit scope).",
                    meta,
                    offending="scope",
                )
            )
        except RuntimeError as e:
            return _result(
                _err(
                    "internal_error",
                    f"git failed: {e}",
                    "Ensure cwd is a git repo and base ref exists.",
                    meta,
                )
            )
        if ctx_data.truncated:
            meta = _meta(
                cwd,
                r.config_mode,
                r.access,
                r.timeout,
                0,
                None,
                scope,
                base,
                effective_paths,
                truncated=True,
                hint=ctx_data.truncation_hint,
                workspace_source=ws_source,
                requested_budget=r.budget,
                redacted_paths=ctx_data.redacted_paths,
            )
            return _result(
                _err(
                    "context_too_large",
                    "The attached diff is too large to review safely.",
                    ctx_data.truncation_hint or "Narrow the scope.",
                    meta,
                )
            )
        meta = _meta(
            cwd,
            r.config_mode,
            r.access,
            r.timeout,
            0,
            None,
            scope,
            base,
            effective_paths,
            workspace_source=ws_source,
            requested_budget=r.budget,
            redacted_paths=ctx_data.redacted_paths,
        )
        if ctx_data.summary.files_changed == 0 and not ctx_data.text.strip():
            return _result(
                _empty_diff_result(
                    "claude_adversarial_review",
                    meta,
                    ctx_data.summary,
                    effective_paths,
                    verdict="unknown",
                    confidence="low",
                )
            )
        context_text, context_summary = ctx_data.text, ctx_data.summary
        redacted_paths = ctx_data.redacted_paths
        payload["paths"] = effective_paths
    out = await _execute(
        "claude_adversarial_review",
        payload,
        r,
        cwd,
        scope=scope,
        base=base,
        paths=effective_paths,
        context_text=context_text,
        context_summary=context_summary,
        workspace_source=ws_source,
        redacted_paths=redacted_paths,
    )
    return _result(out)


# Starting a background job commits to spend (the job runs to completion or its
# best-effort budget stop threshold even if never polled), but returns immediately
# without blocking.
_ASYNC_START_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": True,
    "destructiveHint": False,
    "idempotentHint": False,
}


@mcp.tool(
    annotations=_ASYNC_START_ANNOTATIONS,
    title="Review changes with Claude (background)",
    output_schema=JOB_STARTED_SCHEMA,
)
async def claude_review_changes_async(
    scope: Annotated[Scope, Field(description="working_tree|staged|branch")],
    base: Annotated[str, Field(description="Base ref for scope=branch.")] = "main",
    focus: Annotated[str | None, Field(description="e.g. 'security', 'tests'.")] = None,
    paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional plain repo-relative paths to filter the server-provided diff. "
                "No pathspec magic or excludes; []/omitted means unfiltered."
            )
        ),
    ] = None,
    workspace_root: Annotated[
        str | None,
        Field(
            description="Absolute path to the repo/workspace to operate in. If omitted, "
            "the server uses the client's first MCP root, else its own cwd."
        ),
    ] = None,
    config_mode: Annotated[ConfigMode | None, Field(description="inherit|scoped|safe|bare")] = None,
    access: Annotated[Access | None, Field(description="toolless|readonly")] = None,
    model: Annotated[
        str | None, Field(description="Claude model override; omit for configured default.")
    ] = None,
    effort: Annotated[
        Effort | None, Field(description="Reasoning effort: low|medium|high|xhigh|max.")
    ] = None,
    max_budget_usd: Annotated[
        float | None, Field(description="Per-call Claude spend cap; clamped by server limits.")
    ] = None,
    detail: Annotated[Detail, Field(description="summary|full")] = "summary",
    ctx: Context | None = None,
) -> ToolResult:
    """Launch a git diff review in the background and return a job_id.

    Use when a diff review may outlive the current turn. Paid external call;
    creates local job state and cannot be resumed if cancelled. Poll with
    claude_job_status, read with claude_job_result, delete after reading with
    claude_job_consume_result, or stop with claude_job_cancel. Empty diffs return
    ok:true immediately without starting a job.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    r, err = _resolve(
        config_mode,
        access,
        model,
        max_budget_usd,
        None,
        detail,
        cwd,
        scope=scope,
        base=base,
        paths=paths,
        workspace_source=ws_source,
        effort=effort,
    )
    if err:
        return _result(err)
    # A background job is bounded by its wall-clock deadline, not the synchronous
    # timeout_seconds; report that everywhere so meta stays consistent with the job.
    job_timeout = jobs.max_seconds()
    meta = _meta(
        cwd,
        r.config_mode,
        r.access,
        job_timeout,
        0,
        None,
        scope,
        base,
        paths,
        workspace_source=ws_source,
        requested_budget=r.budget,
    )
    effective_paths, paths_err = _resolve_paths(paths, meta)
    if paths_err:
        return _result(paths_err)
    try:
        ctx_data = await run_sync(
            lambda: gather_context(cwd, scope=scope, base=base, paths=effective_paths)
        )
    except InvalidBaseError:
        return _result(
            _err(
                "invalid_base",
                f"Invalid base ref '{base}'.",
                "Use an existing git ref matching [A-Za-z0-9._/-]+ that does not start with '-'.",
                meta,
                offending="base",
            )
        )
    except InvalidScopeError:
        return _result(
            _err(
                "invalid_scope",
                f"Invalid scope '{scope}'.",
                "Use working_tree, staged, or branch.",
                meta,
                offending="scope",
            )
        )
    except RuntimeError as e:
        return _result(
            _err(
                "internal_error",
                f"git failed: {e}",
                "Ensure cwd is a git repo and base ref exists.",
                meta,
            )
        )
    if ctx_data.truncated:
        meta = _meta(
            cwd,
            r.config_mode,
            r.access,
            job_timeout,
            0,
            None,
            scope,
            base,
            effective_paths,
            truncated=True,
            hint=ctx_data.truncation_hint,
            workspace_source=ws_source,
            requested_budget=r.budget,
            redacted_paths=ctx_data.redacted_paths,
        )
        return _result(
            _err(
                "context_too_large",
                "The diff is too large to review safely.",
                ctx_data.truncation_hint or "Narrow the scope.",
                meta,
            )
        )
    meta = _meta(
        cwd,
        r.config_mode,
        r.access,
        job_timeout,
        0,
        None,
        scope,
        base,
        effective_paths,
        workspace_source=ws_source,
        requested_budget=r.budget,
        redacted_paths=ctx_data.redacted_paths,
    )
    if ctx_data.summary.files_changed == 0 and not ctx_data.text.strip():
        return _result(
            _empty_diff_result("claude_review_changes", meta, ctx_data.summary, effective_paths)
        )
    prompt = build_prompt(
        "claude_review_changes",
        {"scope": scope, "base": base, "focus": focus, "paths": effective_paths},
        ctx_data.text,
    )
    cmd, dropped = build_command(prompt, r.config_mode, r.access, r.model, r.budget, r.effort)
    cfg = JobConfig(
        kind="claude_review_changes",
        config_mode=r.config_mode,
        access=r.access,
        scope=scope,
        base=base,
        detail=r.detail,
        timeout_seconds=jobs.max_seconds(),
        workspace_source=ws_source,
        context_summary=ctx_data.summary,
        requested_max_budget_usd=r.budget,
        paths=effective_paths,
        redacted_paths=ctx_data.redacted_paths,
        security_warnings=hook_security_warnings(cwd, r.config_mode),
    )
    try:
        job_id, started_at = await run_sync(lambda: jobs.start_job(cmd, cwd, cfg, prompt))
    except (FileNotFoundError, PermissionError):
        return _result(
            _err(
                "claude_not_found",
                "The `claude` CLI was not found on PATH.",
                "Install Claude Code and ensure `claude` is on PATH.",
                meta,
            )
        )
    except OSError as e:
        return _result(
            _err(
                "internal_error",
                f"Failed to start async job: {e}",
                "Check the workspace/job-state directory permissions and retry.",
                meta,
            )
        )
    started = JobStarted(
        job_id=job_id,
        kind="claude_review_changes",
        started_at=started_at,
        deadline_seconds=job_timeout,
        poll_after_ms=jobs.poll_after_ms(),
        ttl_seconds=jobs.ttl_seconds(),
        meta=_meta(
            cwd,
            r.config_mode,
            r.access,
            job_timeout,
            0,
            None,
            scope,
            base,
            effective_paths,
            workspace_source=ws_source,
            requested_budget=r.budget,
            redacted_paths=ctx_data.redacted_paths,
            compat_warnings=dropped,
            security_warnings=hook_security_warnings(cwd, r.config_mode),
        ),
    )
    return _result(started.model_dump(mode="json", exclude_none=True))


@mcp.tool(
    annotations=_LOCAL_MUTATION_ANNOTATIONS,
    title="Background job status",
    output_schema=JOB_STATUS_SCHEMA,
)
async def claude_job_status(
    job_id: Annotated[str, Field(description="A job_id from an *_async tool.")],
    workspace_root: Annotated[
        str | None,
        Field(description="Workspace the job belongs to (defaults like the async tools)."),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Check a background review job without fetching the full result.

    Use after claude_review_changes_async. Returns status, elapsed time,
    result_available, polling hints, and cost when available. If
    result_available is true, call claude_job_result.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    data = await run_sync(lambda: jobs.status(cwd, job_id))
    if data is None:
        meta = _meta(cwd, "inherit", "toolless", 0, 0, None, workspace_source=ws_source)
        return _result(
            _err(
                "job_not_found",
                f"No job '{job_id}' in this workspace.",
                "Check the job_id, or start a new job; records expire after the TTL.",
                meta,
                offending="job_id",
            )
        )
    return _result(data)


@mcp.tool(
    annotations=_LOCAL_MUTATION_ANNOTATIONS,
    title="Background job result",
    output_schema=RESULT_SCHEMA,
)
async def claude_job_result(
    job_id: Annotated[str, Field(description="A job_id from an *_async tool.")],
    workspace_root: Annotated[
        str | None,
        Field(description="Workspace the job belongs to (defaults like the async tools)."),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Fetch a finished background review without deleting the job record.

    Use when claude_job_status reports result_available=true. Returns the same
    structured review envelope as claude_review_changes, with meta.job_id set. To
    fetch and delete the stored record, use claude_job_consume_result.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    payload, found = await run_sync(lambda: jobs.result(cwd, job_id, False))
    if not found:
        meta = _meta(cwd, "inherit", "toolless", 0, 0, None, workspace_source=ws_source)
        return _result(
            _err(
                "job_not_found",
                f"No job '{job_id}' in this workspace.",
                "Check the job_id, or start a new job; records expire after the TTL.",
                meta,
                offending="job_id",
            )
        )
    return _result(payload)


@mcp.tool(
    annotations=_LOCAL_MUTATION_ANNOTATIONS,
    title="Consume background job result",
    output_schema=RESULT_SCHEMA,
)
async def claude_job_consume_result(
    job_id: Annotated[str, Field(description="A job_id from an *_async tool.")],
    workspace_root: Annotated[
        str | None,
        Field(description="Workspace the job belongs to (defaults like the async tools)."),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Fetch a finished background review and delete the stored job record.

    Use only when you no longer need to poll or re-read the job. Returns the same
    structured envelope as claude_job_result, then deletes completed job state.
    Non-done jobs are not deleted.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    payload, found = await run_sync(lambda: jobs.result(cwd, job_id, True))
    if not found:
        meta = _meta(cwd, "inherit", "toolless", 0, 0, None, workspace_source=ws_source)
        return _result(
            _err(
                "job_not_found",
                f"No job '{job_id}' in this workspace.",
                "Check the job_id, or start a new job; records expire after the TTL.",
                meta,
                offending="job_id",
            )
        )
    return _result(payload)


@mcp.tool(
    annotations=_LOCAL_MUTATION_ANNOTATIONS,
    title="Cancel background job",
    output_schema=JOB_STATUS_SCHEMA,
)
async def claude_job_cancel(
    job_id: Annotated[str, Field(description="A job_id from an *_async tool.")],
    workspace_root: Annotated[
        str | None,
        Field(description="Workspace the job belongs to (defaults like the async tools)."),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Cancel a running background review job.

    Use to stop a job from claude_review_changes_async. Terminates the Claude
    process and marks the job cancelled; cancelled jobs cannot be resumed.
    Already-terminal jobs are returned unchanged.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    data = await run_sync(lambda: jobs.cancel(cwd, job_id))
    if data is None:
        meta = _meta(cwd, "inherit", "toolless", 0, 0, None, workspace_source=ws_source)
        return _result(
            _err(
                "job_not_found",
                f"No job '{job_id}' in this workspace.",
                "Check the job_id, or start a new job; records expire after the TTL.",
                meta,
                offending="job_id",
            )
        )
    return _result(data)


@mcp.tool(
    annotations=_FREE_READ_ANNOTATIONS,
    title="Preview review context (no spend)",
    output_schema=DRY_RUN_SCHEMA,
)
async def claude_review_dry_run(
    scope: Annotated[Scope, Field(description="working_tree|staged|branch")],
    base: Annotated[str, Field(description="Base ref for scope=branch.")] = "main",
    paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional plain repo-relative paths to filter the previewed diff. "
                "No pathspec magic or excludes; []/omitted means unfiltered."
            )
        ),
    ] = None,
    config_mode: Annotated[ConfigMode | None, Field(description="inherit|scoped|safe|bare")] = None,
    workspace_root: Annotated[
        str | None,
        Field(
            description="Absolute path to the repo/workspace. If omitted, the server "
            "uses the client's first MCP root, else its own cwd."
        ),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Preview what a diff review WOULD send, free and without calling Claude.

    Use before a paid claude_review_changes to confirm the resolved workspace,
    diff byte size, whether it would be truncated, and how many secret-looking
    files would be redacted. Read-only; makes no paid call.
    """
    cwd, ws_err, ws_source = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    dry_config_mode, cm_err = _resolve_config_mode_only(
        config_mode, cwd, scope=scope, base=base, paths=paths, workspace_source=ws_source
    )
    if cm_err:
        return _result(cm_err)
    assert dry_config_mode is not None
    meta = _meta(
        cwd, dry_config_mode, "toolless", 0, 0, None, scope, base, paths, workspace_source=ws_source
    )
    effective_paths, paths_err = _resolve_paths(paths, meta)
    if paths_err:
        return _result(paths_err)
    try:
        ctx_data = await run_sync(
            lambda: gather_context(cwd, scope=scope, base=base, paths=effective_paths)
        )
    except InvalidBaseError:
        return _result(
            _err(
                "invalid_base",
                f"Invalid base ref '{base}'.",
                "Use an existing git ref matching [A-Za-z0-9._/-]+ that does not start with '-'.",
                meta,
                offending="base",
            )
        )
    except InvalidScopeError:
        return _result(
            _err(
                "invalid_scope",
                f"Invalid scope '{scope}'.",
                "Use working_tree, staged, or branch.",
                meta,
                offending="scope",
            )
        )
    except RuntimeError as e:
        return _result(
            _err(
                "internal_error",
                f"git failed: {e}",
                "Ensure cwd is a git repo and base ref exists.",
                meta,
            )
        )
    fs = preflight.flag_support()
    result = DryRunResult(
        cwd=cwd,
        workspace_source=ws_source,
        workspace_warning=workspace_warning_for(ws_source, cwd),
        scope=scope,
        base=base,
        paths=effective_paths or [],
        context_summary=ctx_data.summary,
        diff_bytes=ctx_data.diff_bytes,
        max_diff_bytes=MAX_DIFF_BYTES,
        truncated=ctx_data.truncated,
        truncation_hint=ctx_data.truncation_hint,
        redacted_paths_count=len(ctx_data.redacted_paths),
        redacted_paths=ctx_data.redacted_paths,
        resolved_config_mode=cast("ConfigMode", dry_config_mode),
        hooks_disabled=hooks_disabled_available(dry_config_mode, fs.help_parsed, fs.supported),
        workspace_hook_settings=workspace_hook_settings(cwd),
        security_warnings=hook_security_warnings(cwd, dry_config_mode),
    )
    return _result(result.model_dump(mode="json", exclude_none=True))


@mcp.tool(
    annotations=_LOCAL_MUTATION_ANNOTATIONS,
    title="List background jobs",
    output_schema=JOB_LIST_SCHEMA,
)
async def claude_job_list(
    workspace_root: Annotated[
        str | None,
        Field(description="Workspace whose jobs to list (defaults like the async tools)."),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """List the background review jobs known for this workspace, newest first.

    Use to recover job_ids lost across context compaction or interruption. Returns
    each job's id, kind, status, start time, result_available, expiry, and cost when
    terminal. Like the other lifecycle tools it refreshes statuses (not read-only).
    """
    cwd, ws_err, _ = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root))
    data = await run_sync(lambda: jobs.list_jobs(cwd))
    return _result(data)


@mcp.tool(
    annotations=_FREE_READ_ANNOTATIONS,
    title="Claude CLI status & defaults",
    output_schema=STATUS_SCHEMA,
)
def claude_status() -> ToolResult:
    """Check Claude CLI readiness and resolved defaults before spending.

    Free and read-only. Use first when unsure whether paid tools can run, or to
    inspect config_mode/access/model/effort/budget/timeout defaults.
    """
    found = shutil.which(cli_contract.CLAUDE_BIN) is not None
    version = None
    authenticated: bool | None = None
    auth_detail: str | None = None
    supported: bool | None = None
    version_warning: str | None = None
    flags_warning: str | None = None
    if found:
        try:
            version = subprocess.run(
                [cli_contract.CLAUDE_BIN, *cli_contract.VERSION_ARGS],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
        except Exception:
            version = None
        supported = version_supported(version)
        if supported is False:
            version_warning = (
                f"installed claude version {version!r} is outside this plugin's "
                f"tested major(s) {sorted(supported_majors())}; tools may still work — "
                "file an issue if they do not, or set "
                f"{cli_contract.SUPPORTED_MAJORS_ENV} to silence this"
            )
        # Free auth probe: lets an agent discover a logged-out CLI before
        # spending money on a paid call that would only then fail auth.
        authenticated, auth_detail = auth_status()
        # Free flag-contract probe: warn if a guarantee-bearing flag is missing
        # from `claude --help` (an early drift signal), without gating execution.
        fs = preflight.flag_support()
        missing = preflight.missing_expected_flags(fs)
        if missing:
            flags_warning = (
                "claude --help did not list expected flags: "
                f"{', '.join(missing)}; update Claude Code, or update this plugin "
                "if the warning persists"
            )
    else:
        fs = preflight.FlagSupport(supported=frozenset(), help_parsed=False)
    d = defaults()
    default_errors: list[ErrorInfo] = []
    if d.config_mode not in ("inherit", "scoped", "safe", "bare"):
        default_errors.append(
            ErrorInfo(
                code="unsupported_config_mode",
                message=f"Unknown config_mode '{d.config_mode}'.",
                repair="Set CC_PLUGIN_CODEX_CLAUDE_CONFIG to one of: inherit, scoped, safe, bare.",
                offending_param="config_mode",
            )
        )
    if d.access not in ("toolless", "readonly"):
        default_errors.append(
            ErrorInfo(
                code="unsupported_access",
                message=f"Unknown access '{d.access}'.",
                repair="Set CC_PLUGIN_CODEX_ACCESS to one of: toolless, readonly.",
                offending_param="access",
            )
        )
    if d.config_mode == "safe" and found and not safe_available(fs.help_parsed, fs.supported):
        default_errors.append(
            ErrorInfo(
                code="unsupported_config_mode",
                message="config_mode=safe requires a Claude CLI with --safe-mode support.",
                repair=(
                    "Update Claude Code, or set CC_PLUGIN_CODEX_CLAUDE_CONFIG to "
                    "inherit, scoped, or bare."
                ),
                offending_param="config_mode",
            )
        )
    if d.config_mode == "bare" and found and not bare_available():
        default_errors.append(
            ErrorInfo(
                code="api_key_missing",
                message="config_mode=bare requires ANTHROPIC_API_KEY, which is unset.",
                repair=(
                    "Set ANTHROPIC_API_KEY, or set CC_PLUGIN_CODEX_CLAUDE_CONFIG to "
                    "inherit, scoped, or safe."
                ),
                offending_param="config_mode",
            )
        )
    raw_defaults = RawDefaults(
        config_mode=d.config_mode,
        access=d.access,
        model=d.model,
        effort=d.effort,
        max_budget_usd=d.max_budget_usd,
        timeout_seconds=d.timeout_seconds,
    )
    resolved = ResolvedDefaults(
        config_mode=cast(
            "ConfigMode",
            d.config_mode if d.config_mode in ("inherit", "scoped", "safe", "bare") else "inherit",
        ),
        access=cast("Access", d.access if d.access in ("toolless", "readonly") else "toolless"),
        model=d.model,
        effort=cast("Effort", sanitize_effort(d.effort)),
        max_budget_usd=clamp_budget(d.max_budget_usd),
        timeout_seconds=clamp_timeout(d.timeout_seconds),
        budget_bounds=[MIN_BUDGET_USD, MAX_BUDGET_USD],
        timeout_bounds=[MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS],
        practical_min_budget_hint=PRACTICAL_MIN_BUDGET_HINT,
    )
    status = StatusResult(
        claude_found=found,
        claude_version=version,
        claude_authenticated=authenticated,
        auth_detail=auth_detail,
        version_supported=supported,
        version_warning=version_warning,
        flags_warning=flags_warning,
        # Version is advisory, not gating: a major outside the tested range warns
        # (version_warning) but does not flip ready, so a claude major bump no
        # longer self-blocks an authenticated, installed CLI.
        ready=bool(found and authenticated and not default_errors),
        config_modes_available={
            "inherit": found,
            "scoped": found,
            "safe": found and safe_available(fs.help_parsed, fs.supported),
            "bare": found and bare_available(),
        },
        hooks_disabled=found
        and hooks_disabled_available(resolved.config_mode, fs.help_parsed, fs.supported),
        raw_defaults=raw_defaults,
        resolved_defaults=resolved,
        default_errors=default_errors,
        caveat="config_mode=safe disables Claude Code customizations/hooks while preserving OAuth.",
    )
    return _result(status.model_dump(mode="json", exclude_none=True))


def _capabilities_payload() -> dict:
    """Build the capability contract. Shared by cc_codex_capabilities and its
    claude_capabilities alias so the two tools cannot drift."""

    def tool_detail(
        name: str,
        cost: Literal["free", "paid"],
        use_when: str,
        returns: str,
        required: list[str] | None = None,
        optional: list[str] | None = None,
    ) -> ToolCapability:
        return ToolCapability(
            name=name,
            cost=cost,
            use_when=use_when,
            required_params=required or [],
            key_optional_params=optional or [],
            returns=returns,
        )

    execution_knobs = ["config_mode", "access", "model", "effort", "max_budget_usd"]
    sync_execution_knobs = [*execution_knobs, "timeout_seconds"]

    result = CapabilitiesResult(
        name="cc-plugin-codex",
        version=__version__,
        transport="stdio",
        stability="experimental",
        paid_tools=[
            "claude_ask",
            "claude_review_changes",
            "claude_adversarial_review",
            "claude_review_changes_async",
        ],
        free_tools=[
            "claude_status",
            "cc_codex_capabilities",
            "claude_capabilities",
            "claude_review_dry_run",
            "claude_job_status",
            "claude_job_result",
            "claude_job_consume_result",
            "claude_job_cancel",
            "claude_job_list",
        ],
        tool_details=[
            tool_detail(
                "claude_status",
                "free",
                "Check CLI readiness, auth, version warnings, defaults, and budget guidance.",
                "readiness booleans plus resolved defaults and practical budget hint",
            ),
            tool_detail(
                "claude_review_dry_run",
                "free",
                "Preview diff workspace, size, truncation, redaction, and optional paths "
                "filter before paying.",
                "diff byte count, context summary, truncation state, and redacted paths",
                required=["scope"],
                optional=["base", "paths", "config_mode", "workspace_root"],
            ),
            tool_detail(
                "claude_ask",
                "paid",
                "Ask for a second opinion on a question or design choice.",
                "structured verdict, findings, questions, assumptions, next steps, cost, and usage",
                required=["prompt"],
                optional=[
                    "context",
                    "workspace_root",
                    *sync_execution_knobs,
                ],
            ),
            tool_detail(
                "claude_review_changes",
                "paid",
                "Review working_tree, staged, or branch git diff synchronously; paths "
                "scopes the server-provided diff but not readonly workspace reads.",
                "structured review result; empty diffs return without spending",
                required=["scope"],
                optional=[
                    "base",
                    "focus",
                    "paths",
                    "workspace_root",
                    *sync_execution_knobs,
                ],
            ),
            tool_detail(
                "claude_adversarial_review",
                "paid",
                "Pressure-test a plan, claim, or decision; optionally attach a diff.",
                "structured counterarguments, risks, questions, assumptions, cost, and usage",
                required=["target"],
                optional=[
                    "evidence",
                    "scope",
                    "base",
                    "paths",
                    "workspace_root",
                    *sync_execution_knobs,
                ],
            ),
            tool_detail(
                "claude_review_changes_async",
                "paid",
                "Start a background diff review for long-running reviews; paths scopes the "
                "server-provided diff.",
                "job_id, status, polling hint, deadline, TTL, and resolved meta",
                required=["scope"],
                optional=[
                    "base",
                    "focus",
                    "paths",
                    "workspace_root",
                    *execution_knobs,
                ],
            ),
            tool_detail(
                "claude_job_status",
                "free",
                "Poll a background job without fetching the full result.",
                "job state, result_available, elapsed time, expiry, cost when terminal",
                required=["job_id"],
                optional=["workspace_root"],
            ),
            tool_detail(
                "claude_job_result",
                "free",
                "Fetch a finished background job result without deleting it.",
                "same structured envelope as claude_review_changes, with meta.job_id",
                required=["job_id"],
                optional=["workspace_root"],
            ),
            tool_detail(
                "claude_job_consume_result",
                "free",
                "Fetch and delete a finished background job record.",
                "same structured envelope as claude_job_result; removes terminal state",
                required=["job_id"],
                optional=["workspace_root"],
            ),
            tool_detail(
                "claude_job_cancel",
                "free",
                "Cancel a running background review job.",
                "job status after cancellation or terminal-state refresh",
                required=["job_id"],
                optional=["workspace_root"],
            ),
            tool_detail(
                "claude_job_list",
                "free",
                "Recover job IDs or inspect known jobs for a workspace.",
                "compact job summaries newest first",
                optional=["workspace_root"],
            ),
        ],
        config_modes=["inherit", "scoped", "safe", "bare"],
        access_modes=["toolless", "readonly"],
        scope=[
            "independent code review of a git diff",
            "adversarial review of a plan/claim",
            "a free-form independent second opinion",
            "background diff review with poll/result/cancel for long runs",
            "a free dry-run preview of workspace, diff size, and redaction before paying",
        ],
        negative_scope=[
            "does NOT grant write or Bash tools; Claude Code hooks can run outside the "
            "tool allowlist in inherit/scoped, so use safe or bare for untrusted workspaces",
            "does NOT act as a general Claude chat",
            "does NOT proxy Claude's own MCP tools",
            "does NOT resume a call once it ends or is cancelled",
            "does NOT guarantee secret removal; diff redaction is best-effort and "
            "access=readonly lets Claude read workspace files directly",
        ],
        prerequisites=[
            "the `claude` CLI installed and authenticated",
            "git, for the diff-bearing tools",
            "ANTHROPIC_API_KEY only for config_mode=bare",
        ],
        deprecation_policy=(
            "Deprecated tools remain discoverable during their compatibility window "
            "with replacement guidance; removals/renames and schema/error changes "
            "bump the fingerprint."
        ),
    )
    return result.model_dump(mode="json", exclude_none=True)


@mcp.tool(
    annotations=_FREE_READ_ANNOTATIONS,
    title="cc-plugin-codex capabilities",
    output_schema=CAPABILITIES_SCHEMA,
)
def cc_codex_capabilities() -> ToolResult:
    """Return the compact capability contract for this server.

    Free and read-only. Call first when unsure which tool to use. Includes tool
    inventory, scope/negative-scope, prerequisites, modes, deprecation policy, and
    fingerprint. Also available as claude_capabilities.
    """
    return _result(_capabilities_payload())


@mcp.tool(
    annotations=_FREE_READ_ANNOTATIONS,
    title="Claude review capabilities",
    output_schema=CAPABILITIES_SCHEMA,
)
def claude_capabilities() -> ToolResult:
    """Alias of cc_codex_capabilities: the Claude review/critique capability contract.

    Free and read-only. Discoverable under a claude_* name; returns the identical
    contract as cc_codex_capabilities.
    """
    return _result(_capabilities_payload())


@mcp.resource("cc-plugin-codex://capabilities")
def capabilities() -> str:
    """Server capability summary, negative scope, and prerequisites."""
    return CAPABILITY_SUMMARY


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    main()
