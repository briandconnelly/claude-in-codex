"""FastMCP server exposing Claude Code as bounded, read-only critique tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Annotated, Literal, cast, get_args
from urllib.parse import unquote, urlparse
from uuid import uuid4

from anyio.to_thread import run_sync
from fastmcp import Context, FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from pydantic import Field, ValidationError

from claude_in_codex import __version__, cli_contract, jobs, preflight
from claude_in_codex.claude import (
    auth_status,
    build_command,
    classify_failure,
    run_claude_async,
)
from claude_in_codex.claude_models import read_model_catalog
from claude_in_codex.config import (
    ENV_PLACEHOLDER_REPAIR,
    MAX_BUDGET_USD,
    MAX_TIMEOUT_SECONDS,
    MIN_BUDGET_USD,
    MIN_TIMEOUT_SECONDS,
    VALID_EFFORTS,
    api_key_present,
    bare_available,
    clamp_budget,
    clamp_timeout,
    defaults,
    hook_security_warnings,
    hooks_disabled_available,
    is_env_placeholder,
    max_input_bytes,
    placeholder_env_vars,
    safe_available,
    sanitize_effort,
    supported_majors,
    version_supported,
    workspace_hook_settings,
)
from claude_in_codex.context import (
    MAX_DIFF_BYTES,
    GitUnavailableError,
    InvalidBaseError,
    InvalidHeadError,
    InvalidPathsError,
    InvalidScopeError,
    NotAGitRepoError,
    gather_context,
    normalize_paths,
)
from claude_in_codex.jobs import JobConfig
from claude_in_codex.normalize import apply_cost_usage, build_prompt, normalize_envelope
from claude_in_codex.schemas import (
    CAPABILITIES_SCHEMA,
    DEFAULT_NEXT_STEP,
    DRY_RUN_SCHEMA,
    FINGERPRINT,
    FINGERPRINT_COVERS,
    JOB_LIST_SCHEMA,
    JOB_STARTED_SCHEMA,
    JOB_STATUS_SCHEMA,
    MODEL_CATALOG_SCHEMA,
    RESULT_SCHEMA,
    STATUS_SCHEMA,
    Access,
    AsyncLifecycle,
    CapabilitiesResult,
    Confidence,
    ConfigMode,
    Detail,
    DryRunResult,
    Effort,
    ErrorCode,
    ErrorCodeDoc,
    ErrorDetails,
    ErrorInfo,
    ErrorResult,
    JobId,
    JobStarted,
    Meta,
    RawDefaults,
    RawResponse,
    RepairAction,
    ResolvedDefaults,
    Scope,
    StatusResult,
    SuccessResult,
    ToolCapability,
    Verdict,
    branch_range,
    workspace_warning_for,
)

CAPABILITY_SUMMARY = (
    "claude-in-codex lets Codex ask Claude Code for bounded critique: diff reviews, "
    "adversarial plan review, and second opinions. The server grants no Bash/write "
    "tools and never proxies Claude's own MCP tools, but workspace hooks may run "
    "shell in config_mode=inherit or config_mode=scoped; config_mode=safe and "
    "config_mode=bare disable hooks. Paid tools send context to Anthropic; call "
    "claude_status before spending. Use claude_models to discover valid model slugs. "
    "claude_review_changes blocks; "
    "claude_review_changes_async runs in background with poll/result/cancel; "
    "claude_review_dry_run previews diff-size/redaction. "
    "scope=branch reviews base...head locally; no ref fetch, GitHub, or PR URLs. "
    "workspace_root defaults to first MCP root else cwd; with roots must be inside. "
    "toolless default; readonly lets Claude read files, bypassing diff redaction. "
    "Tool semantic and argument-validation failures return isError:true with an "
    "ok:false envelope (code/message/repair) in structuredContent. "
    "Free-form input capped by CLAUDE_IN_CODEX_MAX_INPUT_BYTES. Experimental; pin fingerprint."
)

_HEAD_FIELD_DESC = (
    "Head ref for scope=branch; reviews base...head instead of base...HEAD. Only "
    "valid for scope=branch; defaults to HEAD. Must be a local-resolvable git ref "
    "or commit — the server does not fetch refs, call GitHub, or accept PR URLs."
)

PRACTICAL_MIN_BUDGET_HINT = (
    "The configured clamp allows $0.01+, but real paid calls usually need about "
    "$0.10-$0.20 even for small prompts; lower budgets may spend and still return "
    "budget_exceeded."
)

_BUDGET_DESCRIPTION = (
    "Best-effort Claude spend threshold ($0.01-$5.00); omit for configured default."
)

mcp = FastMCP(name="claude-in-codex", instructions=CAPABILITY_SUMMARY)

# readOnlyHint tracks observable effects, disclosed via annotations_policy in
# claude_capabilities. Paid tools spend money and send context to an external
# service (Anthropic), so they are advertised non-read-only to keep client
# confirmation in the loop. Static annotations represent the worst case across
# config modes: inherit/scoped may execute arbitrary workspace hooks, including
# destructive shell commands, so paid calls are destructive even though the
# server itself grants no Bash/write tools. Each call spends, so it is also
# non-idempotent. Job-lifecycle polling tools perform lazy maintenance while
# reading (deadline kill, TTL deletion), so they are also non-read-only even
# though they never alter a terminal job's stored result.
_PAID_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": True,
    "destructiveHint": True,
    "idempotentHint": False,
}
_FREE_READ_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}
# Job lifecycle calls perform lazy maintenance while reading: polling enforces the
# job deadline (an overdue job is killed and marked timeout) and deletes
# TTL-expired records — observable mutations, so they are not read-only. They
# never alter a terminal job's stored result.
_JOB_LIFECYCLE_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
}
# Cancel mutates local job state; repeating it returns the terminal job unchanged.
_JOB_CANCEL_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
}
# Consume irreversibly deletes the stored result record (a second call cannot re-fetch it).
_JOB_CONSUME_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
}
# Starting a background job commits to spend and creates persistent local job
# state: the job runs to completion or its best-effort budget stop threshold
# even if never polled, but the launch returns immediately without blocking.
_ASYNC_START_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": True,
    "destructiveHint": True,
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
    configured_budget: float | None = None,
    effective_budget: float | None = None,
    redacted_paths: list[str] | None = None,
    compat_warnings: list[str] | None = None,
    security_warnings: list[str] | None = None,
    *,
    head: str | None = None,
) -> Meta:
    # head is keyword-only so the many positional _meta(...) call sites that pass
    # base positionally stay untouched; only branch-scope call sites set it.
    effective_head, diff_range = branch_range(scope, base, head)
    return Meta(
        cwd=cwd,
        config_mode=cast("ConfigMode", config_mode),
        access=cast("Access", access),
        scope=scope,
        base=base,
        head=effective_head,
        diff_range=diff_range,
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
        configured_max_budget_usd=configured_budget,
        effective_max_budget_usd=effective_budget,
        redacted_paths=redacted_paths or [],
        compat_warnings=compat_warnings or [],
        security_warnings=security_warnings or [],
    )


# Ceiling on the reconstructed repair call. Above it the corrected arguments are
# omitted rather than echoed, so an oversized input cannot be returned twice.
REPAIR_ARGS_MAX_BYTES = 8192
# Ceiling on the echoed rejected value in ErrorDetails.value.
DETAIL_VALUE_MAX_CHARS = 200


def _render_value(value: object) -> str | None:
    """The rejected value as a bounded string, for ErrorDetails.value.

    None is dropped rather than rendered as "None": an absent detail field means
    "not applicable", and a caller that genuinely passed null learns that from the
    message, not from a string that is indistinguishable from a literal."""
    if value is None:
        return None
    text = value if isinstance(value, str) else repr(value)
    if len(text) > DETAIL_VALUE_MAX_CHARS:
        return text[:DETAIL_VALUE_MAX_CHARS] + "…"
    return text


def _err(
    code: str,
    message: str,
    repair: str,
    meta: Meta,
    offending: str | None = None,
    retryable: bool = False,
    *,
    allowed_values: list[str] | None = None,
    details: ErrorDetails | None = None,
    action: RepairAction | None = None,
    retry_after_ms: int | None = None,
) -> dict:
    """Build the ok:false envelope.

    `offending`/`allowed_values` are folded into the typed `details` block so call
    sites stay terse while the wire shape keeps one home for recovery data. An
    explicitly passed `details` wins for any field it sets."""
    merged = (details or ErrorDetails()).model_copy(
        update={
            k: v
            for k, v in (("field", offending), ("allowed_values", allowed_values))
            if v is not None and getattr(details, k, None) is None
        }
    )
    return ErrorResult(
        error=ErrorInfo(
            code=cast("ErrorCode", code),
            message=message,
            repair=repair,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            details=merged if merged.model_dump(exclude_none=True) else None,
            action=action,
        ),
        meta=meta,
    ).model_dump(mode="json", exclude_none=True)


class ValidationEnvelopeMiddleware(Middleware):
    """Return the ok:false envelope for pre-handler argument-validation failures.

    Without this, FastMCP converts a pydantic ValidationError into isError:true
    with prose-only content — no code, no repair, no structuredContent — an
    undisclosed third error carrier. FastMCP's own argument-coercion errors carry
    a pydantic ValidationError whose `.title` is `call[<tool_name>]`; a
    ValidationError raised by a tool body's internal model construction instead
    has the model class name as its `.title`. That `call[...]` prefix is what
    distinguishes an argument-shape failure from an internal bug — only the
    former is converted into this envelope."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        try:
            return await call_next(context)
        except ValidationError as exc:
            if not exc.title.startswith("call["):
                raise  # internal model bug, not an argument-shape failure
            first = exc.errors()[0]
            loc = first.get("loc") or ("arguments",)
            field = ".".join(str(part) for part in loc)
            expected = (first.get("ctx") or {}).get("expected")
            message = f"Invalid argument '{field}': {first.get('msg', 'invalid value')}."
            if expected:
                repair = f"Set {field} to one of: {expected}, then retry the same call."
            else:
                repair = (
                    f"Fix the '{field}' argument to match the tool's inputSchema, "
                    "then retry the same call."
                )
            allowed = await self._allowed_values(context, loc)
            # `input` is the rejected value — except for a missing required
            # argument, where pydantic reports the whole arguments dict. Echoing
            # that as details.value would name every argument as the offender.
            # (FastMCP reports "missing_argument"; pydantic's own is "missing".)
            missing = str(first.get("type") or "").startswith("missing")
            rejected = None if missing else first.get("input")
            # Placeholder meta: arguments never validated, so no resolved
            # cwd/config exists for this call — the error block is the contract.
            meta = _meta("", "inherit", "toolless", 0, 0, None)
            return _result(
                _err(
                    "invalid_arguments",
                    message,
                    repair,
                    meta,
                    offending=field,
                    allowed_values=allowed,
                    details=ErrorDetails(value=_render_value(rejected)),
                    action=self._repair_action(context, loc),
                )
            )

    @staticmethod
    def _repair_action(context, loc) -> RepairAction:
        """A corrected call carrying every still-valid original argument.

        Only the invalid top-level argument is dropped — the agent fills it back
        in — so a long prompt or context never has to be restated. Arguments are
        omitted entirely (leaving a bare retry_with_changes) when they exceed
        REPAIR_ARGS_MAX_BYTES, so a repair block cannot become the largest thing in
        the response; that bound is published as argument_reconstruction.

        The echo is deliberate and bounded rather than filtered: no tool here takes
        a credential-shaped argument (the free-text ones are prompt/context/
        evidence/target/focus, which the caller just wrote), and the values go back
        to that same caller in the same response. Filtering them would make the
        repair non-callable, which is the whole point of the block. If a
        secret-bearing argument is ever added, exclude it here — an argument that
        must not be echoed must not be reconstructed."""
        name = getattr(getattr(context, "message", None), "name", None)
        args = getattr(getattr(context, "message", None), "arguments", None)
        if not name or not isinstance(args, dict) or not loc:
            return RepairAction(next_step="retry_with_changes")
        remaining = {k: v for k, v in args.items() if k != str(loc[0])}
        try:
            size = len(json.dumps(remaining, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            return RepairAction(next_step="retry_with_changes", tool=str(name))
        if size > REPAIR_ARGS_MAX_BYTES:
            return RepairAction(next_step="retry_with_changes", tool=str(name))
        return RepairAction(next_step="retry_with_changes", tool=str(name), arguments=remaining)

    @staticmethod
    async def _allowed_values(context, loc) -> list[str] | None:
        """Enum choices for the failing argument, from the tool's published
        inputSchema — structural, not parsed from pydantic's prose."""
        name = getattr(getattr(context, "message", None), "name", None)
        if not name or not loc:
            return None
        try:
            tool = await mcp.get_tool(str(name))
        except Exception:
            return None
        prop = (getattr(tool, "parameters", None) or {}).get("properties", {}).get(str(loc[0]))
        if not isinstance(prop, dict):
            return None
        enum = prop.get("enum")
        if isinstance(enum, list) and all(isinstance(v, str) for v in enum):
            return enum
        return None


mcp.add_middleware(ValidationEnvelopeMiddleware())


def _invalid_paths_error(meta: Meta, message: str | None = None) -> dict:
    return _err(
        "invalid_paths",
        message or "Invalid paths filter.",
        "Pass plain repo-relative paths such as paths=['src', 'tests/test_context.py']; "
        "omit paths or pass [] for an unfiltered diff.",
        meta,
        offending="paths",
    )


def _job_not_found_error(job_id: str, meta: Meta) -> dict:
    """Jobs are per-workspace, so the repair call must pin the SAME workspace the
    lookup used — listing under a differently-resolved workspace would show an
    unrelated (usually empty) set and read as confirmation that the job is gone."""
    return _err(
        "job_not_found",
        f"No job '{job_id}' in this workspace.",
        "Check the job_id, or start a new job; records expire after the TTL.",
        meta,
        details=ErrorDetails(field="job_id", value=job_id, reason="unknown_or_expired"),
        action=RepairAction(
            next_step="call_tool",
            tool="claude_job_list",
            arguments={"workspace_root": meta.cwd},
        ),
    )


_INVALID_HEAD_REPAIR = (
    "Pass a local-resolvable git ref or commit for head and only with scope=branch; "
    "omit head to compare against HEAD. The server does not fetch refs, call GitHub, "
    "or accept PR numbers/URLs — make the ref available locally first."
)


_INVALID_BASE_REPAIR = (
    "Use an existing git ref matching [A-Za-z0-9._/-]+ that does not start with '-'."
)


def _invalid_base_error(meta: Meta, base: str | None) -> dict:
    return _err(
        "invalid_base",
        f"Invalid base ref '{base}'.",
        _INVALID_BASE_REPAIR,
        meta,
        offending="base",
    )


def _invalid_head_error(meta: Meta, message: str | None = None) -> dict:
    return _err(
        "invalid_head",
        message or "Invalid head ref.",
        _INVALID_HEAD_REPAIR,
        meta,
        offending="head",
    )


def _invalid_scope_error(meta: Meta, scope: str | None, *, scope_optional: bool = False) -> dict:
    repair = "Use working_tree, staged, or branch."
    if scope_optional:
        repair = "Use working_tree, staged, or branch (or omit scope)."
    return _err(
        "invalid_scope",
        f"Invalid scope '{scope}'.",
        repair,
        meta,
        offending="scope",
        allowed_values=["working_tree", "staged", "branch"],
    )


def _context_error_result(
    exc: Exception,
    meta: Meta,
    *,
    scope: str | None,
    base: str | None,
    head: str | None,
    scope_optional: bool = False,
) -> dict:
    if isinstance(exc, InvalidBaseError):
        return _invalid_base_error(meta, base)
    if isinstance(exc, InvalidHeadError):
        return _invalid_head_error(meta, f"Invalid head ref '{head}'.")
    if isinstance(exc, InvalidScopeError):
        return _invalid_scope_error(meta, scope, scope_optional=scope_optional)
    if isinstance(exc, NotAGitRepoError):
        return _err(
            "not_a_git_repo",
            "Workspace is not a git repository.",
            "Run reviews from inside a git repository, or pass workspace_root pointing at one.",
            meta,
        )
    if isinstance(exc, GitUnavailableError):
        return _err(
            "git_unavailable",
            "Git executable is not available.",
            "Install git and ensure it is on PATH.",
            meta,
        )
    return _err(
        "internal_error",
        f"git failed: {exc}",
        "Ensure cwd is a git repo and base ref exists.",
        meta,
    )


def _resolve_paths(paths: list[str] | None, meta: Meta) -> tuple[list[str] | None, dict | None]:
    try:
        return normalize_paths(paths), None
    except InvalidPathsError as exc:
        return None, _invalid_paths_error(meta, str(exc))


def _workspace_error(
    code: str, workspace_root: str | None = None, roots: list[str] | None = None
) -> dict:
    """Build the ok:false envelope for a workspace-resolution failure.

    `roots` is the snapshot the resolver already fetched, not a fresh lookup:
    workspace_outside_roots is only reachable because that snapshot was non-empty,
    so re-asking the client would add a round-trip that could fail or answer
    differently and strip the very repair data this error exists to carry."""
    meta = _meta("", "inherit", "toolless", 0, 0, None)
    if code == "workspace_outside_roots":
        return _err(
            code,
            f"workspace_root '{workspace_root}' is outside the client's MCP roots.",
            "Pass a workspace_root contained by an MCP root, omit workspace_root to "
            "use the first root, or configure the intended directory as a root.",
            meta,
            details=ErrorDetails(
                field="workspace_root",
                value=workspace_root,
                reason="outside_mcp_roots",
                allowed_roots=roots or None,
            ),
            action=RepairAction(
                next_step="retry_with_changes",
                arguments={"workspace_root": roots[0]} if roots else None,
            ),
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
    path is None and source is None. `roots` is the snapshot used for the
    containment check, returned so the error builder can name the allowed roots
    without asking the client again."""
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
        return None, "invalid_workspace_root", None, roots
    if workspace_root and roots and not any(_contained_by(path, root) for root in roots):
        return None, "workspace_outside_roots", None, roots
    return path, None, source, roots


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
        "CLAUDE_IN_CODEX_MAX_INPUT_BYTES if this workspace intentionally allows it.",
        meta,
        details=ErrorDetails(
            field=largest,
            reason="user_input_over_limit",
            limit_bytes=limit,
            actual_bytes=total,
        ),
        action=RepairAction(next_step="retry_with_changes"),
    )


def _oversized_diff_details(ctx_data) -> ErrorDetails:
    """Typed sizes for a diff that blew the gathered-context cap.

    diff_bytes is the pre-truncation size of the redacted diff, so the pair is
    directly comparable and an agent can compute how much to narrow by."""
    return ErrorDetails(
        reason="diff_over_limit",
        max_diff_bytes=MAX_DIFF_BYTES,
        diff_bytes=ctx_data.diff_bytes,
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
    requested_budget: float | None
    configured_budget: float | None
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
    head: str | None = None,
):
    """Resolve env defaults, clamp environment values, and validate explicit values.

    Returns (Resolved, None) or (None, error_dict).
    """
    d = defaults()
    cm = config_mode or d.config_mode
    ac = access or d.access
    mdl = model or d.model
    requested_budget = max_budget_usd
    configured_budget = d.max_budget_usd if max_budget_usd is None else None
    budget = max_budget_usd if max_budget_usd is not None else clamp_budget(d.max_budget_usd)
    timeout = clamp_timeout(timeout_seconds if timeout_seconds is not None else d.timeout_seconds)
    det = detail if detail in ("summary", "full") else "summary"
    eff = effort if effort in VALID_EFFORTS else d.effort

    # FastMCP enforces these bounds from each paid tool's inputSchema. Keep a
    # defensive check here too so direct/internal callers can never silently
    # raise or lower an explicit caller cap by bypassing argument validation.
    if max_budget_usd is not None and not (MIN_BUDGET_USD <= max_budget_usd <= MAX_BUDGET_USD):
        safe_meta = _meta(
            cwd,
            cm if cm in ("inherit", "scoped", "safe", "bare") else "inherit",
            ac if ac in ("toolless", "readonly") else "toolless",
            timeout,
            0,
            None,
            scope,
            base,
            paths,
            workspace_source=workspace_source,
            requested_budget=requested_budget,
            configured_budget=configured_budget,
            head=head,
        )
        return None, _err(
            "invalid_arguments",
            f"max_budget_usd must be between {MIN_BUDGET_USD} and {MAX_BUDGET_USD} inclusive.",
            "Set max_budget_usd within the published inputSchema bounds, then retry.",
            safe_meta,
            offending="max_budget_usd",
        )

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
            requested_budget=requested_budget,
            configured_budget=configured_budget,
            effective_budget=budget,
            head=head,
        )
        return None, _err(
            "unsupported_config_mode",
            f"Unknown config_mode '{cm}'.",
            "Use one of: inherit, scoped, safe, bare.",
            safe_meta,
            offending="config_mode",
            allowed_values=["inherit", "scoped", "safe", "bare"],
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
            requested_budget=requested_budget,
            configured_budget=configured_budget,
            effective_budget=budget,
            head=head,
        )
        return None, _err(
            "unsupported_access",
            f"Unknown access '{ac}'.",
            "Use one of: toolless, readonly.",
            safe_meta,
            offending="access",
            allowed_values=["toolless", "readonly"],
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
                requested_budget=requested_budget,
                configured_budget=configured_budget,
                effective_budget=budget,
                head=head,
            )
            return None, _err(
                "unsupported_config_mode",
                "config_mode=safe requires a Claude CLI with --safe-mode support.",
                "Update Claude Code, or use config_mode inherit/scoped/bare.",
                safe_meta,
                offending="config_mode",
                allowed_values=["inherit", "scoped", "bare"],
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
        requested_budget=requested_budget,
        configured_budget=configured_budget,
        effective_budget=budget,
        security_warnings=hook_security_warnings(cwd, cm),
        head=head,
    )
    if cm == "bare" and not bare_available():
        return None, _err(
            "api_key_missing",
            "config_mode=bare requires ANTHROPIC_API_KEY, which is unset.",
            "Set ANTHROPIC_API_KEY, or use config_mode inherit/scoped/safe.",
            meta,
            offending="config_mode",
        )
    return Resolved(
        cm,
        ac,
        mdl,
        requested_budget,
        configured_budget,
        budget,
        timeout,
        det,
        eff,
    ), None


def _resolve_config_mode_only(
    config_mode: str | None,
    cwd: str,
    scope: str | None = None,
    base: str | None = None,
    paths: list[str] | None = None,
    workspace_source: str | None = None,
    head: str | None = None,
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
        head=head,
    )
    if cm not in ("inherit", "scoped", "safe", "bare"):
        return None, _err(
            "unsupported_config_mode",
            f"Unknown config_mode '{cm}'.",
            "Use one of: inherit, scoped, safe, bare.",
            meta,
            offending="config_mode",
            allowed_values=["inherit", "scoped", "safe", "bare"],
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
                allowed_values=["inherit", "scoped", "bare"],
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
    head: str | None = None,
) -> dict:
    prompt = build_prompt(tool, payload, context_text)
    cmd, dropped = build_command(prompt, r.config_mode, r.access, r.model, r.budget, r.effort)
    run = await run_claude_async(
        cmd,
        cwd=cwd,
        timeout_seconds=r.timeout,
        stdin_text=prompt,
        config_mode=r.config_mode,
    )
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
        requested_budget=r.requested_budget,
        configured_budget=r.configured_budget,
        effective_budget=r.budget,
        redacted_paths=redacted_paths,
        compat_warnings=dropped,
        security_warnings=hook_security_warnings(cwd, r.config_mode),
        head=head,
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
        info = classify_failure(run, config_mode=r.config_mode)
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
        float | None,
        Field(ge=MIN_BUDGET_USD, le=MAX_BUDGET_USD, description=_BUDGET_DESCRIPTION),
    ] = None,
    timeout_seconds: Annotated[
        int | None, Field(description="Sync call timeout; omit for configured default.")
    ] = None,
    detail: Annotated[Detail, Field(description="summary|full")] = "summary",
    ctx: Context | None = None,
) -> ToolResult:
    """Get Claude's view on a question or design choice; not for diffs or attacks.

    Paid; sends context to Anthropic; blocks to timeout_seconds; cancellable;
    input is size-capped before spend. The server grants no Bash/write tools;
    workspace hooks may run shell in config_mode=inherit or config_mode=scoped.
    config_mode=safe and config_mode=bare disable hooks.

    Egress: prompt/context and access=readonly reads are verbatim; reply
    redaction is best effort.
    """
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
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
        requested_budget=r.requested_budget,
        configured_budget=r.configured_budget,
        effective_budget=r.budget,
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
    head: Annotated[str | None, Field(description=_HEAD_FIELD_DESC)] = None,
    focus: Annotated[str | None, Field(description="e.g. 'security', 'tests'.")] = None,
    paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional plain repo-relative paths to filter the server-provided diff. "
                "No exclude/pathspec magic; shell-style wildcards (*, ?, []) still "
                "glob recursively. []/omitted means unfiltered."
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
        float | None,
        Field(ge=MIN_BUDGET_USD, le=MAX_BUDGET_USD, description=_BUDGET_DESCRIPTION),
    ] = None,
    timeout_seconds: Annotated[
        int | None, Field(description="Sync call timeout; omit for configured default.")
    ] = None,
    detail: Annotated[Detail, Field(description="summary|full")] = "summary",
    ctx: Context | None = None,
) -> ToolResult:
    """Review a working_tree, staged, or branch git diff with Claude (blocking).

    Paid; sends context to Anthropic; blocks to timeout_seconds; cancellable;
    empty diffs skip spend. The server grants no Bash/write tools; workspace
    hooks may run shell in config_mode=inherit or config_mode=scoped.
    config_mode=safe and config_mode=bare disable hooks.

    Egress: redaction is best effort for gathered diff/output, not free-form
    inputs or access=readonly reads.
    """
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
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
        head=head,
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
        requested_budget=r.requested_budget,
        configured_budget=r.configured_budget,
        effective_budget=r.budget,
        head=head,
    )
    if head is not None and scope != "branch":
        return _result(
            _invalid_head_error(meta, f"head is only valid for scope=branch, not '{scope}'.")
        )
    effective_paths, paths_err = _resolve_paths(paths, meta)
    if paths_err:
        return _result(paths_err)
    try:
        ctx_data = await run_sync(
            lambda: gather_context(cwd, scope=scope, base=base, paths=effective_paths, head=head)
        )
    except (InvalidBaseError, InvalidHeadError, InvalidScopeError, RuntimeError) as exc:
        return _result(_context_error_result(exc, meta, scope=scope, base=base, head=head))
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
            requested_budget=r.requested_budget,
            configured_budget=r.configured_budget,
            effective_budget=r.budget,
            redacted_paths=ctx_data.redacted_paths,
            head=head,
        )
        return _result(
            _err(
                "context_too_large",
                "The diff is too large to review safely.",
                ctx_data.truncation_hint or "Narrow the scope.",
                meta,
                details=_oversized_diff_details(ctx_data),
                action=RepairAction(next_step="retry_with_changes"),
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
        requested_budget=r.requested_budget,
        configured_budget=r.configured_budget,
        effective_budget=r.budget,
        redacted_paths=ctx_data.redacted_paths,
        security_warnings=hook_security_warnings(cwd, r.config_mode),
        head=head,
    )
    if ctx_data.summary.files_changed == 0 and not ctx_data.text.strip():
        return _result(
            _empty_diff_result("claude_review_changes", meta, ctx_data.summary, effective_paths)
        )
    out = await _execute(
        "claude_review_changes",
        {"scope": scope, "base": base, "head": head, "focus": focus, "paths": effective_paths},
        r,
        cwd,
        scope=scope,
        base=base,
        paths=effective_paths,
        context_text=ctx_data.text,
        context_summary=ctx_data.summary,
        workspace_source=ws_source,
        head=head,
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
    head: Annotated[str | None, Field(description=_HEAD_FIELD_DESC)] = None,
    paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional plain repo-relative paths for the attached server-provided diff. "
                "Requires scope; no exclude/pathspec magic; shell-style wildcards "
                "(*, ?, []) still glob recursively. []/omitted means unfiltered."
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
        float | None,
        Field(ge=MIN_BUDGET_USD, le=MAX_BUDGET_USD, description=_BUDGET_DESCRIPTION),
    ] = None,
    timeout_seconds: Annotated[
        int | None, Field(description="Sync call timeout; omit for configured default.")
    ] = None,
    detail: Annotated[Detail, Field(description="summary|full")] = "summary",
    ctx: Context | None = None,
) -> ToolResult:
    """Have Claude attack a plan or decision; optionally attach a diff.

    Paid; sends context to Anthropic; blocks to timeout_seconds; cancellable;
    empty diffs skip spend. The server grants no Bash/write tools; workspace
    hooks may run shell in config_mode=inherit or config_mode=scoped.
    config_mode=safe and config_mode=bare disable hooks.

    Egress: redaction is best effort for gathered diff/output, not free-form
    inputs or access=readonly reads.
    """
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
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
        head=head,
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
        requested_budget=r.requested_budget,
        configured_budget=r.configured_budget,
        effective_budget=r.budget,
        head=head,
    )
    if paths and not scope:
        return _result(
            _invalid_paths_error(meta, "paths requires scope on claude_adversarial_review.")
        )
    # head only makes sense for an attached branch diff; reject it when scope is
    # omitted or is not branch (this also covers adversarial head-without-scope).
    if head is not None and scope != "branch":
        return _result(
            _invalid_head_error(
                meta,
                "head requires scope=branch on claude_adversarial_review.",
            )
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
            requested_budget=r.requested_budget,
            configured_budget=r.configured_budget,
            effective_budget=r.budget,
            head=head,
        )
        try:
            ctx_data = await run_sync(
                lambda: gather_context(
                    cwd, scope=scope, base=base, paths=effective_paths, head=head
                )
            )
        except (InvalidBaseError, InvalidHeadError, InvalidScopeError, RuntimeError) as exc:
            return _result(
                _context_error_result(
                    exc,
                    meta,
                    scope=scope,
                    base=base,
                    head=head,
                    scope_optional=True,
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
                requested_budget=r.requested_budget,
                configured_budget=r.configured_budget,
                effective_budget=r.budget,
                redacted_paths=ctx_data.redacted_paths,
                head=head,
            )
            return _result(
                _err(
                    "context_too_large",
                    "The attached diff is too large to review safely.",
                    ctx_data.truncation_hint or "Narrow the scope.",
                    meta,
                    details=_oversized_diff_details(ctx_data),
                    action=RepairAction(next_step="retry_with_changes"),
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
            requested_budget=r.requested_budget,
            configured_budget=r.configured_budget,
            effective_budget=r.budget,
            redacted_paths=ctx_data.redacted_paths,
            head=head,
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
        payload["scope"] = scope
        payload["base"] = base
        payload["head"] = head
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
        head=head,
    )
    return _result(out)


async def _idempotent_match(cwd: str, idempotency_key: str | None) -> dict | None:
    """Launch dedupe: JobStatus of a live/unexpired job started with this key, or
    None. This fast path runs once at entry as a cheap early return. The
    pre-spawn leg (in claude_review_changes_async, just before jobs.start_job) is
    now an atomic on-disk reservation via jobs.reserve_idempotency_key, which
    publishes a fully-written marker via an atomic os.link, so same-key launches
    cannot double-spawn on one local filesystem — this fast path is advisory
    only. NFS-style filesystems without atomic hardlinks remain a caveat."""
    if not idempotency_key:
        return None
    existing = await run_sync(lambda: jobs.find_by_idempotency_key(cwd, idempotency_key))
    if existing is None:
        return None
    return await run_sync(lambda: jobs.status(cwd, existing))


@mcp.tool(
    annotations=_ASYNC_START_ANNOTATIONS,
    title="Review changes with Claude (background)",
    output_schema=JOB_STARTED_SCHEMA,
)
async def claude_review_changes_async(
    scope: Annotated[Scope, Field(description="working_tree|staged|branch")],
    base: Annotated[str, Field(description="Base ref for scope=branch.")] = "main",
    head: Annotated[str | None, Field(description=_HEAD_FIELD_DESC)] = None,
    focus: Annotated[str | None, Field(description="e.g. 'security', 'tests'.")] = None,
    paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional plain repo-relative paths to filter the server-provided diff. "
                "No exclude/pathspec magic; shell-style wildcards (*, ?, []) still "
                "glob recursively. []/omitted means unfiltered."
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
        float | None,
        Field(ge=MIN_BUDGET_USD, le=MAX_BUDGET_USD, description=_BUDGET_DESCRIPTION),
    ] = None,
    detail: Annotated[Detail, Field(description="summary|full")] = "summary",
    idempotency_key: Annotated[
        str | None,
        Field(
            description="Optional client-chosen key making launch retry-safe "
            "(atomic per workspace via an on-disk reservation): if a "
            "job with this key already exists in this workspace (within the job "
            "TTL), its status is returned instead of starting a duplicate paid "
            "job. After a dropped connection, retry with the same key or check "
            "claude_job_list before re-launching. The key alone determines the "
            "match; do not reuse a key with different arguments — the existing "
            "job's status is returned unchanged."
        ),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Launch a git diff review in the background and return a job_id.

    Paid; sends context to Anthropic; empty diffs skip spend; idempotency_key
    avoids duplicate-launch spend. The server grants
    no Bash/write tools; workspace hooks may run shell in config_mode=inherit or
    config_mode=scoped. config_mode=safe and config_mode=bare disable hooks.

    Egress: redaction is best effort for gathered diff/output, not free-form
    inputs or access=readonly reads.
    """
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
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
        head=head,
    )
    if err:
        return _result(err)
    match = await _idempotent_match(cwd, idempotency_key)
    if match is not None:
        return _result(match)
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
        requested_budget=r.requested_budget,
        configured_budget=r.configured_budget,
        effective_budget=r.budget,
        head=head,
    )
    if head is not None and scope != "branch":
        return _result(
            _invalid_head_error(meta, f"head is only valid for scope=branch, not '{scope}'.")
        )
    effective_paths, paths_err = _resolve_paths(paths, meta)
    if paths_err:
        return _result(paths_err)
    try:
        ctx_data = await run_sync(
            lambda: gather_context(cwd, scope=scope, base=base, paths=effective_paths, head=head)
        )
    except (InvalidBaseError, InvalidHeadError, InvalidScopeError, RuntimeError) as exc:
        return _result(_context_error_result(exc, meta, scope=scope, base=base, head=head))
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
            requested_budget=r.requested_budget,
            configured_budget=r.configured_budget,
            effective_budget=r.budget,
            redacted_paths=ctx_data.redacted_paths,
            head=head,
        )
        return _result(
            _err(
                "context_too_large",
                "The diff is too large to review safely.",
                ctx_data.truncation_hint or "Narrow the scope.",
                meta,
                details=_oversized_diff_details(ctx_data),
                action=RepairAction(next_step="retry_with_changes"),
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
        requested_budget=r.requested_budget,
        configured_budget=r.configured_budget,
        effective_budget=r.budget,
        redacted_paths=ctx_data.redacted_paths,
        head=head,
    )
    if ctx_data.summary.files_changed == 0 and not ctx_data.text.strip():
        return _result(
            _empty_diff_result("claude_review_changes", meta, ctx_data.summary, effective_paths)
        )
    prompt = build_prompt(
        "claude_review_changes",
        {"scope": scope, "base": base, "head": head, "focus": focus, "paths": effective_paths},
        ctx_data.text,
    )
    cmd, dropped = build_command(prompt, r.config_mode, r.access, r.model, r.budget, r.effort)
    cfg = JobConfig(
        kind="claude_review_changes",
        config_mode=r.config_mode,
        access=r.access,
        scope=scope,
        base=base,
        head=head,
        detail=r.detail,
        timeout_seconds=jobs.max_seconds(),
        workspace_source=ws_source,
        context_summary=ctx_data.summary,
        requested_max_budget_usd=r.requested_budget,
        configured_max_budget_usd=r.configured_budget,
        effective_max_budget_usd=r.budget,
        paths=effective_paths,
        redacted_paths=ctx_data.redacted_paths,
        security_warnings=hook_security_warnings(cwd, r.config_mode),
        idempotency_key=idempotency_key,
    )
    reserved_job_id: str | None = None
    if idempotency_key:
        candidate = uuid4().hex
        holder = await run_sync(
            lambda: jobs.reserve_idempotency_key(cwd, idempotency_key, candidate)
        )
        if holder is not None:
            data = await run_sync(lambda: jobs.status(cwd, holder))
            if data is not None:
                return _result(data)
            # Holder vanished between reserve and status (crashed pre-spawn and
            # reaped): fall through and launch fresh under a new reservation.
            holder2 = await run_sync(
                lambda: jobs.reserve_idempotency_key(cwd, idempotency_key, candidate)
            )
            if holder2 is not None:
                data = await run_sync(lambda: jobs.status(cwd, holder2))
                if data is not None:
                    return _result(data)
                return _result(
                    _err(
                        "internal_error",
                        "idempotency_key reservation is held by a job that has no record.",
                        "Retry, or omit idempotency_key to force a new launch.",
                        meta,
                        offending="idempotency_key",
                        retryable=True,
                    )
                )
            reserved_job_id = candidate
        else:
            reserved_job_id = candidate
    try:
        job_id, started_at = await run_sync(
            lambda: jobs.start_job(cmd, cwd, cfg, prompt, job_id=reserved_job_id)
        )
    except (FileNotFoundError, PermissionError):
        if idempotency_key and reserved_job_id:
            await run_sync(
                lambda: jobs.release_idempotency_key(cwd, idempotency_key, reserved_job_id)
            )
        return _result(
            _err(
                "claude_not_found",
                "The `claude` CLI was not found on PATH.",
                "Install Claude Code and ensure `claude` is on PATH.",
                meta,
            )
        )
    except OSError as e:
        if idempotency_key and reserved_job_id:
            await run_sync(
                lambda: jobs.release_idempotency_key(cwd, idempotency_key, reserved_job_id)
            )
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
            requested_budget=r.requested_budget,
            configured_budget=r.configured_budget,
            effective_budget=r.budget,
            redacted_paths=ctx_data.redacted_paths,
            compat_warnings=dropped,
            security_warnings=hook_security_warnings(cwd, r.config_mode),
            head=head,
        ),
    )
    return _result(started.model_dump(mode="json", exclude_none=True))


@mcp.tool(
    annotations=_JOB_LIFECYCLE_ANNOTATIONS,
    title="Background job status",
    output_schema=JOB_STATUS_SCHEMA,
)
async def claude_job_status(
    job_id: Annotated[
        JobId,
        Field(description="A 32-character lowercase hexadecimal job_id from an *_async tool."),
    ],
    workspace_root: Annotated[
        str | None,
        Field(description="Workspace the job belongs to (defaults like the async tools)."),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Check a background review job without fetching the full result. Polling
    performs lazy maintenance: an overdue job is killed and marked timeout, and
    TTL-expired records are deleted; a terminal job's stored result is never
    altered.

    Use after claude_review_changes_async. Returns status, elapsed time,
    result_available, polling hints, and cost when available. If
    result_available is true, call claude_job_result.
    """
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
    data = await run_sync(lambda: jobs.status(cwd, job_id))
    if data is None:
        meta = _meta(cwd, "inherit", "toolless", 0, 0, None, workspace_source=ws_source)
        return _result(_job_not_found_error(job_id, meta))
    return _result(data)


@mcp.tool(
    annotations=_JOB_LIFECYCLE_ANNOTATIONS,
    title="Background job result",
    output_schema=RESULT_SCHEMA,
)
async def claude_job_result(
    job_id: Annotated[
        JobId,
        Field(description="A 32-character lowercase hexadecimal job_id from an *_async tool."),
    ],
    workspace_root: Annotated[
        str | None,
        Field(description="Workspace the job belongs to (defaults like the async tools)."),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Fetch a finished background review without deleting the job record.
    Polling performs lazy maintenance: an overdue job is killed and marked
    timeout, and TTL-expired records are deleted; a terminal job's stored
    result is never altered.

    Use when claude_job_status reports result_available=true. Returns the same
    structured envelope as claude_review_changes, with meta.job_id set. Use
    claude_job_consume_result to fetch and delete instead.
    """
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
    payload, found = await run_sync(lambda: jobs.result(cwd, job_id, False))
    if not found:
        meta = _meta(cwd, "inherit", "toolless", 0, 0, None, workspace_source=ws_source)
        return _result(_job_not_found_error(job_id, meta))
    return _result(payload)


@mcp.tool(
    annotations=_JOB_CONSUME_ANNOTATIONS,
    title="Consume background job result",
    output_schema=RESULT_SCHEMA,
)
async def claude_job_consume_result(
    job_id: Annotated[
        JobId,
        Field(description="A 32-character lowercase hexadecimal job_id from an *_async tool."),
    ],
    workspace_root: Annotated[
        str | None,
        Field(description="Workspace the job belongs to (defaults like the async tools)."),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult:
    """Fetch a finished background review and delete the stored job record.

    Use only when you no longer need to poll or re-read the job. Returns the same
    structured envelope as claude_job_result, then deletes completed job state.
    Non-done jobs are not deleted. Deletion is irreversible; the result cannot be
    re-fetched afterward.
    """
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
    payload, found = await run_sync(lambda: jobs.result(cwd, job_id, True))
    if not found:
        meta = _meta(cwd, "inherit", "toolless", 0, 0, None, workspace_source=ws_source)
        return _result(_job_not_found_error(job_id, meta))
    return _result(payload)


@mcp.tool(
    annotations=_JOB_CANCEL_ANNOTATIONS,
    title="Cancel background job",
    output_schema=JOB_STATUS_SCHEMA,
)
async def claude_job_cancel(
    job_id: Annotated[
        JobId,
        Field(description="A 32-character lowercase hexadecimal job_id from an *_async tool."),
    ],
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
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
    data = await run_sync(lambda: jobs.cancel(cwd, job_id))
    if data is None:
        meta = _meta(cwd, "inherit", "toolless", 0, 0, None, workspace_source=ws_source)
        return _result(_job_not_found_error(job_id, meta))
    return _result(data)


@mcp.tool(
    annotations=_FREE_READ_ANNOTATIONS,
    title="Preview review context (no spend)",
    output_schema=DRY_RUN_SCHEMA,
)
async def claude_review_dry_run(
    scope: Annotated[Scope, Field(description="working_tree|staged|branch")],
    base: Annotated[str, Field(description="Base ref for scope=branch.")] = "main",
    head: Annotated[str | None, Field(description=_HEAD_FIELD_DESC)] = None,
    paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional plain repo-relative paths to filter the previewed diff. "
                "No exclude/pathspec magic; shell-style wildcards (*, ?, []) still "
                "glob recursively. []/omitted means unfiltered."
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
    cwd, ws_err, ws_source, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
    dry_config_mode, cm_err = _resolve_config_mode_only(
        config_mode, cwd, scope=scope, base=base, paths=paths, workspace_source=ws_source, head=head
    )
    if cm_err:
        return _result(cm_err)
    assert dry_config_mode is not None
    meta = _meta(
        cwd,
        dry_config_mode,
        "toolless",
        0,
        0,
        None,
        scope,
        base,
        paths,
        workspace_source=ws_source,
        head=head,
    )
    if head is not None and scope != "branch":
        return _result(
            _invalid_head_error(meta, f"head is only valid for scope=branch, not '{scope}'.")
        )
    effective_paths, paths_err = _resolve_paths(paths, meta)
    if paths_err:
        return _result(paths_err)
    try:
        ctx_data = await run_sync(
            lambda: gather_context(cwd, scope=scope, base=base, paths=effective_paths, head=head)
        )
    except (InvalidBaseError, InvalidHeadError, InvalidScopeError, RuntimeError) as exc:
        return _result(_context_error_result(exc, meta, scope=scope, base=base, head=head))
    fs = preflight.flag_support()
    effective_head, diff_range = branch_range(scope, base, head)
    result = DryRunResult(
        cwd=cwd,
        workspace_source=ws_source,
        workspace_warning=workspace_warning_for(ws_source, cwd),
        scope=scope,
        base=base,
        head=effective_head,
        diff_range=diff_range,
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
    annotations=_JOB_LIFECYCLE_ANNOTATIONS,
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
    terminal. Listing performs lazy maintenance: an overdue job is killed and marked
    timeout, and TTL-expired records are deleted; a terminal job's stored result is
    never altered.
    """
    cwd, ws_err, _, ws_roots = await _resolve_workspace(workspace_root, ctx)
    if ws_err:
        return _result(_workspace_error(ws_err, workspace_root, ws_roots))
    data = await run_sync(lambda: jobs.list_jobs(cwd))
    return _result(data)


def _default_config_errors(d, found, fs) -> list[ErrorInfo]:
    """Validation errors for the resolved env defaults, for claude_status.

    Root-cause diagnostic first: if the host delivered literal `${...}` values, a
    per-knob "Unknown config_mode '${...}'" message would blame the value instead
    of the host not expanding env substitutions. Flag the placeholders and skip
    the misleading per-knob enum errors for those same vars (a placeholder API key
    is caught here too, even though it is non-empty)."""
    errors: list[ErrorInfo] = []
    placeholders = placeholder_env_vars()
    if placeholders:
        named = ", ".join(placeholders)
        errors.append(
            ErrorInfo(
                code="unexpanded_env_placeholder",
                message=f"These env vars are literal ${{...}} placeholders: {named}.",
                repair=ENV_PLACEHOLDER_REPAIR,
            )
        )
    config_is_placeholder = "CLAUDE_IN_CODEX_CLAUDE_CONFIG" in placeholders
    access_is_placeholder = "CLAUDE_IN_CODEX_ACCESS" in placeholders
    if d.config_mode not in ("inherit", "scoped", "safe", "bare") and not config_is_placeholder:
        errors.append(
            ErrorInfo(
                code="unsupported_config_mode",
                message=f"Unknown config_mode '{d.config_mode}'.",
                repair="Set CLAUDE_IN_CODEX_CLAUDE_CONFIG to one of: inherit, scoped, safe, bare.",
                details=ErrorDetails(
                    field="config_mode",
                    value=str(d.config_mode),
                    allowed_values=["inherit", "scoped", "safe", "bare"],
                ),
                action=RepairAction(next_step="fix_environment"),
            )
        )
    if d.access not in ("toolless", "readonly") and not access_is_placeholder:
        errors.append(
            ErrorInfo(
                code="unsupported_access",
                message=f"Unknown access '{d.access}'.",
                repair="Set CLAUDE_IN_CODEX_ACCESS to one of: toolless, readonly.",
                details=ErrorDetails(
                    field="access",
                    value=str(d.access),
                    allowed_values=["toolless", "readonly"],
                ),
                action=RepairAction(next_step="fix_environment"),
            )
        )
    if d.config_mode == "safe" and found and not safe_available(fs.help_parsed, fs.supported):
        errors.append(
            ErrorInfo(
                code="unsupported_config_mode",
                message="config_mode=safe requires a Claude CLI with --safe-mode support.",
                repair=(
                    "Update Claude Code, or set CLAUDE_IN_CODEX_CLAUDE_CONFIG to "
                    "inherit, scoped, or bare."
                ),
                details=ErrorDetails(
                    field="config_mode",
                    value="safe",
                    reason="unsupported_by_installed_cli",
                    allowed_values=["inherit", "scoped", "bare"],
                ),
                action=RepairAction(next_step="fix_environment"),
            )
        )
    if d.config_mode == "bare" and found and not bare_available():
        errors.append(
            ErrorInfo(
                code="api_key_missing",
                message="config_mode=bare requires ANTHROPIC_API_KEY, which is unset.",
                repair=(
                    "Set ANTHROPIC_API_KEY, or set CLAUDE_IN_CODEX_CLAUDE_CONFIG to "
                    "inherit, scoped, or safe."
                ),
                details=ErrorDetails(field="config_mode", value="bare", reason="api_key_missing"),
                action=RepairAction(next_step="fix_environment"),
            )
        )
    return errors


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
    d = defaults()
    auth_probe_config_mode = (
        d.config_mode if d.config_mode in ("inherit", "scoped", "safe", "bare") else "inherit"
    )
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
        authenticated, auth_detail = auth_status(config_mode=auth_probe_config_mode)
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
    default_errors = _default_config_errors(d, found, fs)
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
    ready = bool(found and authenticated and not default_errors)
    if ready:
        readiness_detail = (
            "ready: installed, authenticated, and defaults are usable for paid calls."
        )
    elif not found:
        readiness_detail = "not ready: the `claude` CLI was not found on PATH."
    elif default_errors:
        readiness_detail = (
            "not ready: default configuration is invalid; inspect default_errors before paid calls."
        )
    elif authenticated is False:
        readiness_detail = "not ready: Claude CLI reports no authenticated session."
    elif authenticated is None:
        readiness_detail = "not ready: Claude CLI authentication could not be determined."
    else:
        readiness_detail = "not ready: inspect status fields before paid calls."

    # Login modes strip ANTHROPIC_API_KEY from the subprocess env (it is used only
    # in bare), so a key set there has no effect — surface that as advisory. Skip a
    # literal ${...} placeholder, which _default_config_errors already diagnoses as
    # unexpanded_env_placeholder; warning again would just duplicate it.
    key_present = api_key_present()
    api_key_warning: str | None = None
    if (
        key_present
        and resolved.config_mode in ("inherit", "scoped", "safe")
        and not is_env_placeholder(os.environ.get("ANTHROPIC_API_KEY"))
    ):
        api_key_warning = (
            "ANTHROPIC_API_KEY is set but ignored in config_mode inherit/scoped/safe "
            "(these use your OAuth login); it is used only in config_mode=bare. Unset "
            "it if it is stale, or use config_mode=bare to use it deliberately."
        )

    status = StatusResult(
        claude_found=found,
        claude_version=version,
        claude_authenticated=authenticated,
        auth_detail=auth_detail,
        version_supported=supported,
        version_warning=version_warning,
        flags_warning=flags_warning,
        api_key_present=key_present,
        api_key_warning=api_key_warning,
        # Version is advisory, not gating: a major outside the tested range warns
        # (version_warning) but does not flip ready, so a claude major bump no
        # longer self-blocks an authenticated, installed CLI.
        ready=ready,
        readiness_detail=readiness_detail,
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


# Per-tool error branch maps (#60). Grouped by the stage that raises them, so a
# tool's set is the union of the stages it actually runs. tests/test_server.py
# cross-checks these against a static walk of the real _err/ErrorInfo call sites.

# Any tool that takes arguments: ValidationEnvelopeMiddleware runs before the body.
_ARG_ERRORS = ["invalid_arguments"]
# Any tool that resolves a workspace directory.
_WORKSPACE_ERRORS = ["invalid_workspace_root", "workspace_outside_roots"]
# Resolving the run configuration for a call that will launch claude.
_CONFIG_ERRORS = ["unsupported_config_mode", "unsupported_access", "api_key_missing"]
# Gathering a git diff (and the size cap that applies to gathered context).
_GIT_ERRORS = [
    "invalid_scope",
    "invalid_base",
    "invalid_head",
    "invalid_paths",
    "not_a_git_repo",
    "git_unavailable",
    "context_too_large",
]
# Launching claude and interpreting what came back.
_CLAUDE_ERRORS = [
    "claude_not_found",
    "claude_auth_required",
    "api_key_invalid",
    "claude_permission_error",
    "budget_exceeded",
    "timeout",
    "nonzero_exit",
    "invalid_json",
    "cli_contract_changed",
]
# Looking a background job up by id in a workspace.
_JOB_LOOKUP_ERRORS = ["job_not_found"]
# Fetching the result of a job that did not finish with an envelope.
_JOB_NONDONE_ERRORS = ["job_running", "job_cancelled", "job_timeout", "job_failed"]
_INTERNAL_ERRORS = ["internal_error"]

# Everything a paid call can fail on BEFORE claude produces output. The async
# starter stops here: it returns a job handle, so every completion-time code
# surfaces later through claude_job_result, not from the start call.
_PAID_PREFLIGHT_ERRORS = [
    *_ARG_ERRORS,
    *_WORKSPACE_ERRORS,
    *_CONFIG_ERRORS,
    # The only launch failure the starter reports itself: the executable is
    # missing, so no job is ever spawned.
    "claude_not_found",
    *_INTERNAL_ERRORS,
]
_PAID_SYNC_ERRORS = [*_PAID_PREFLIGHT_ERRORS, *_CLAUDE_ERRORS]
_JOB_LIFECYCLE_ERRORS = [*_ARG_ERRORS, *_WORKSPACE_ERRORS, *_JOB_LOOKUP_ERRORS]

_TOOL_ERROR_CODES: dict[str, list[str]] = {
    # No arguments and no workspace, so no ok:false envelope is possible. These
    # codes reach the caller through StatusResult.default_errors instead — a
    # different carrier, but the same codes and the same recovery contract.
    "claude_status": [*_CONFIG_ERRORS, "unexpanded_env_placeholder"],
    "claude_capabilities": [],
    "claude_models": [],
    "claude_review_dry_run": [
        *_ARG_ERRORS,
        *_WORKSPACE_ERRORS,
        "unsupported_config_mode",
        *_GIT_ERRORS,
        *_INTERNAL_ERRORS,
    ],
    # No diff gathering: context_too_large here is the user-supplied-text cap.
    "claude_ask": [*_PAID_SYNC_ERRORS, "context_too_large"],
    "claude_review_changes": [*_PAID_SYNC_ERRORS, *_GIT_ERRORS],
    "claude_adversarial_review": [*_PAID_SYNC_ERRORS, *_GIT_ERRORS],
    # Preflight only: a started job's own failures arrive via claude_job_result.
    "claude_review_changes_async": [*_PAID_PREFLIGHT_ERRORS, *_GIT_ERRORS],
    "claude_job_status": _JOB_LIFECYCLE_ERRORS,
    "claude_job_cancel": _JOB_LIFECYCLE_ERRORS,
    # The fetched envelope is the original tool's, so its failure codes surface
    # here too, alongside the not-done lifecycle codes.
    "claude_job_result": [
        *_JOB_LIFECYCLE_ERRORS,
        *_JOB_NONDONE_ERRORS,
        *_CLAUDE_ERRORS,
        *_INTERNAL_ERRORS,
    ],
    "claude_job_consume_result": [
        *_JOB_LIFECYCLE_ERRORS,
        *_JOB_NONDONE_ERRORS,
        *_CLAUDE_ERRORS,
        *_INTERNAL_ERRORS,
    ],
    "claude_job_list": [*_ARG_ERRORS, *_WORKSPACE_ERRORS],
}

# code -> (condition, ever-retryable, ErrorDetails fields it may populate).
# next_step is NOT listed here: it comes from schemas.DEFAULT_NEXT_STEP, which is
# the same table the error envelope itself uses, so the published default and the
# emitted default cannot disagree.
_ERROR_CATALOG: list[tuple[str, str, bool, list[str]]] = [
    ("claude_not_found", "The `claude` executable is not on PATH.", False, []),
    (
        "claude_auth_required",
        "claude is installed but not logged in for the resolved config_mode.",
        False,
        [],
    ),
    (
        "api_key_missing",
        "config_mode=bare was resolved but ANTHROPIC_API_KEY is unset.",
        False,
        ["field", "value", "reason"],
    ),
    ("api_key_invalid", "ANTHROPIC_API_KEY is set but the API rejected it.", False, []),
    (
        "unsupported_config_mode",
        "config_mode is not one of the four modes, or =safe on a CLI without --safe-mode.",
        False,
        ["field", "value", "reason", "allowed_values"],
    ),
    (
        "unsupported_access",
        "access is not toolless or readonly.",
        False,
        ["field", "value", "allowed_values"],
    ),
    (
        "unexpanded_env_placeholder",
        "A tracked env var arrived as a literal ${...}; the host did not expand it.",
        False,
        [],
    ),
    (
        "invalid_arguments",
        "An argument failed the tool's inputSchema before the body ran.",
        False,
        ["field", "value", "allowed_values"],
    ),
    (
        "invalid_scope",
        "scope is not working_tree, staged, or branch.",
        False,
        ["field", "allowed_values"],
    ),
    ("invalid_base", "base is not a locally resolvable git ref.", False, ["field"]),
    (
        "invalid_head",
        "head is not locally resolvable, or was passed without scope=branch.",
        False,
        ["field"],
    ),
    (
        "invalid_paths",
        "paths is not a list of plain repo-relative paths.",
        False,
        ["field"],
    ),
    (
        "invalid_workspace_root",
        "The resolved workspace is not an existing absolute directory.",
        False,
        ["field"],
    ),
    (
        "workspace_outside_roots",
        "workspace_root is not contained by any client-supplied MCP root.",
        False,
        ["field", "value", "reason", "allowed_roots"],
    ),
    (
        "not_a_git_repo",
        "The resolved workspace is not inside a git repository.",
        False,
        [],
    ),
    ("git_unavailable", "git is not on PATH.", False, []),
    (
        "context_too_large",
        "User-supplied text exceeded the input cap, or the gathered diff exceeded the diff cap.",
        False,
        ["field", "reason", "limit_bytes", "actual_bytes", "max_diff_bytes", "diff_bytes"],
    ),
    ("timeout", "claude did not finish within timeout_seconds.", True, []),
    (
        "budget_exceeded",
        "claude hit the best-effort max-budget stop threshold. Replaying it unchanged "
        "spends again and stops the same way.",
        False,
        [],
    ),
    (
        "claude_permission_error",
        "claude was denied a permission the run needed.",
        False,
        [],
    ),
    (
        "nonzero_exit",
        "claude exited non-zero or reported an error result. Rate-limit and overload "
        "cases are marked retryable on the error itself.",
        True,
        [],
    ),
    ("invalid_json", "claude's output was not the expected JSON envelope.", False, []),
    (
        "cli_contract_changed",
        "The installed claude rejected a flag or value this plugin sends.",
        False,
        [],
    ),
    ("internal_error", "An unexpected server-side failure.", True, ["field"]),
    (
        "job_not_found",
        "No job with that id in the resolved workspace (wrong id, wrong workspace, "
        "or the record expired).",
        False,
        ["field", "value", "reason"],
    ),
    (
        "job_running",
        "The job has not finished; no result exists yet. The same fetch succeeds once it does.",
        True,
        ["field", "value"],
    ),
    (
        "job_cancelled",
        "The job was cancelled before producing a result.",
        False,
        ["field", "value"],
    ),
    (
        "job_timeout",
        "The job passed its wall-clock deadline and was reaped.",
        False,
        ["field", "value"],
    ),
    (
        "job_failed",
        "The job's process ended without writing a result envelope.",
        True,
        ["field", "value"],
    ),
]


_ASYNC_LIFECYCLE = AsyncLifecycle(
    start_tools=["claude_review_changes_async"],
    status_tool="claude_job_status",
    result_tool="claude_job_result",
    consume_tool="claude_job_consume_result",
    cancel_tool="claude_job_cancel",
    list_tool="claude_job_list",
    handle_param="job_id",
    poll_delay_field="poll_after_ms",
    result_ready_field="result_available",
    state_field="status",
    running_states=["running"],
    terminal_states=["done", "failed", "cancelled", "timeout"],
    nonresult_terminal_codes=["job_failed", "job_cancelled", "job_timeout"],
    notes=[
        "This server predates MCP's native task support; the lifecycle is these "
        "tools, not tasks/* requests.",
        "Jobs are scoped to the resolved workspace: pass the same workspace_root to "
        "every lifecycle call, or a valid job_id reads as job_not_found.",
        "Wait at least poll_after_ms between claude_job_status calls.",
        "Fetch the result only once result_available is true; fetching earlier "
        "returns an ok:false envelope with job_running.",
        "A started job keeps running and keeps spending if you never poll it; "
        "claude_job_cancel is the only way to stop it early.",
        "An expired record is deleted, so it reports job_not_found rather than a "
        "distinct expired state.",
    ],
)


def _capabilities_payload() -> dict:
    """Build the capability contract. Shared by claude_capabilities."""

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
            # Deduped and sorted so the branch map is stable and comparable; the
            # groups it is composed from deliberately overlap.
            error_codes=sorted(set(_TOOL_ERROR_CODES[name])),
        )

    execution_knobs = ["config_mode", "access", "model", "effort", "max_budget_usd"]
    sync_execution_knobs = [*execution_knobs, "timeout_seconds"]

    result = CapabilitiesResult(
        name="claude-in-codex",
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
            "claude_capabilities",
            "claude_review_dry_run",
            "claude_job_status",
            "claude_job_result",
            "claude_job_consume_result",
            "claude_job_cancel",
            "claude_job_list",
            "claude_models",
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
                optional=["base", "head", "paths", "config_mode", "workspace_root"],
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
                "Review working_tree, staged, or branch git diff synchronously; "
                "scope=branch reviews base...head (head defaults to HEAD); paths "
                "scopes the server-provided diff but not readonly workspace reads.",
                "structured review result; empty diffs return without spending",
                required=["scope"],
                optional=[
                    "base",
                    "head",
                    "focus",
                    "paths",
                    "workspace_root",
                    *sync_execution_knobs,
                ],
            ),
            tool_detail(
                "claude_adversarial_review",
                "paid",
                "Pressure-test a plan, claim, or decision; optionally attach a diff "
                "(scope=branch attaches base...head, head defaults to HEAD).",
                "structured counterarguments, risks, questions, assumptions, cost, and usage",
                required=["target"],
                optional=[
                    "evidence",
                    "scope",
                    "base",
                    "head",
                    "paths",
                    "workspace_root",
                    *sync_execution_knobs,
                ],
            ),
            tool_detail(
                "claude_review_changes_async",
                "paid",
                "Start a background diff review for long-running reviews; scope=branch "
                "reviews base...head (head defaults to HEAD); paths scopes the "
                "server-provided diff.",
                "job_id, status, polling hint, deadline, TTL, and resolved meta",
                required=["scope"],
                optional=[
                    "base",
                    "head",
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
            tool_detail(
                "claude_models",
                "free",
                "Discover valid `model` slugs (aliases + pinned full IDs) before "
                "overriding model on a paid call.",
                "advisory static model catalog; same payload as the claude-in-codex://models "
                "resource; not fingerprint-stable",
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
            "does NOT fetch refs, call the GitHub API, or accept PR numbers/URLs; "
            "scope=branch base/head must already resolve locally",
        ],
        # Published once here so the advertised output schemas can carry the
        # compact error branch instead of inlining this enum 11 times.
        error_codes=sorted(get_args(ErrorCode)),
        error_catalog=[
            ErrorCodeDoc(
                code=code,
                condition=condition,
                next_step=DEFAULT_NEXT_STEP[code],
                ever_retryable=ever_retryable,
                detail_fields=detail_fields,
            )
            for code, condition, ever_retryable, detail_fields in _ERROR_CATALOG
        ],
        argument_reconstruction=(
            "On invalid_arguments the action carries the original call with only the "
            f"invalid argument removed, so you refill one field. Above {REPAIR_ARGS_MAX_BYTES} "
            "bytes of remaining arguments it omits them and names only the tool, so a "
            "large prompt is never echoed back. Those arguments are your own inputs "
            "returned verbatim to you — no tool here takes a credential argument — but "
            "treat the block as input-grade content if your transcript is retained "
            "differently from your requests. Other codes name a follow-up tool call "
            "(action.tool + action.arguments) or no call at all — always branch on "
            "action.next_step first."
        ),
        async_lifecycle=_ASYNC_LIFECYCLE,
        data_egress=(
            "Paid tools (claude_ask, claude_review_changes, claude_adversarial_review, "
            "claude_review_changes_async) send context to Anthropic via the `claude` CLI. "
            "Best-effort secret redaction is applied to the server-gathered git diff before "
            "it is sent AND to the returned model output relayed back (summary, findings, "
            "questions, assumptions, next_steps, raw response text, and error messages). It "
            "does NOT cover your free-form inputs (prompt, context, target, evidence, focus), "
            "which are sent verbatim, nor files Claude reads directly from the workspace "
            "under access=readonly, whose contents the `claude` CLI sends to Anthropic "
            "outside this redaction path. Use access=toolless and config_mode=safe/bare for "
            "sensitive workspaces; redaction is defense-in-depth, not a guarantee."
        ),
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
        annotations_policy=(
            "Static annotations represent the worst case across config modes. "
            "readOnlyHint tracks observable effects: paid tools (claude_ask, "
            "claude_review_changes, claude_adversarial_review, "
            "claude_review_changes_async) spend money and send context to "
            "Anthropic, so they are not read-only; their destructiveHint is true "
            "because config_mode=inherit or config_mode=scoped may execute "
            "workspace hooks with arbitrary shell commands, while config_mode=safe "
            "and config_mode=bare disable hooks; claude_job_status/"
            "claude_job_result/claude_job_list perform lazy maintenance while "
            "reading (deadline kills, TTL deletion) and are not read-only, though "
            "they never alter a terminal job's stored result; "
            "claude_job_consume_result irreversibly deletes the stored record "
            "(destructiveHint true); claude_job_cancel mutates job state but is "
            "idempotent — already-terminal jobs are returned unchanged "
            "(idempotentHint true); claude_status, claude_capabilities, "
            "claude_models, and claude_review_dry_run are pure reads."
        ),
        fingerprint_covers=list(FINGERPRINT_COVERS),
    )
    return result.model_dump(mode="json", exclude_none=True)


@mcp.tool(
    annotations=_FREE_READ_ANNOTATIONS,
    title="Claude review capabilities",
    output_schema=CAPABILITIES_SCHEMA,
)
def claude_capabilities() -> ToolResult:
    """Return the compact capability contract for this server.

    Free and read-only. Call first when unsure which tool to use. Includes tool
    inventory, scope/negative-scope, prerequisites, modes, deprecation policy, and
    fingerprint.
    """
    return _result(_capabilities_payload())


def _model_catalog_payload() -> dict:
    """Single source for the claude_models tool and claude-in-codex://models resource so their
    payloads cannot drift."""
    return read_model_catalog().model_dump(mode="json", exclude_none=True)


@mcp.tool(
    annotations=_FREE_READ_ANNOTATIONS,
    title="List Claude model slugs",
    output_schema=MODEL_CATALOG_SCHEMA,
)
def claude_models() -> ToolResult:
    """List Claude model slugs you can pass as `model`. Free — no model call.

    Advisory, bundled-static list. Prefer alias slugs (kind="alias", e.g.
    'opus'/'sonnet'), which track the latest model, over pinned full IDs. The
    `claude` CLI is the run-time authority: an unlisted slug may work and a
    listed one may be unavailable to your account. Same payload as the
    claude-in-codex://models resource. Not fingerprint-stable.
    """
    return _result(_model_catalog_payload())


@mcp.resource("claude-in-codex://models", mime_type="application/json")
def claude_models_resource() -> dict:
    """Advisory Claude model catalog (same payload as the claude_models tool)."""
    return _model_catalog_payload()


@mcp.resource("claude://models", mime_type="application/json")
def claude_models_resource_deprecated() -> dict:
    """DEPRECATED alias of claude-in-codex://models (same payload); it remains
    available for a compatibility window per the deprecation policy."""
    return _model_catalog_payload()


@mcp.resource("claude-in-codex://capabilities")
def capabilities() -> str:
    """Server capability summary, negative scope, and prerequisites."""
    return CAPABILITY_SUMMARY


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    main()
