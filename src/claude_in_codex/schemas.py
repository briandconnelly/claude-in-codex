"""Pydantic models for the normalized tool result contract."""

from __future__ import annotations

import json
from typing import Annotated, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# Bump this whenever the agent-visible surface changes: tool names, input or
# output schemas, the ErrorCode set, the config_mode/access/scope/detail/effort
# value sets, or the capability guarantees in CAPABILITY_SUMMARY. Clients cache by it.
FINGERPRINT = "claude-in-codex/0.1/schema-31"

# Agent-readable disclosure of what the fingerprint covers. Keep in sync with the
# bump rules in the comment above and the pinned surface in tests/test_fingerprint.py.
FINGERPRINT_COVERS = [
    "tool records (names, descriptions, titles, annotations, input/output schemas)",
    "resource and resource-template records and prompt scaffolds",
    "error-code catalog",
    "config_mode/access/scope/detail/effort value sets",
    "capability summary and capabilities payload",
]

Severity = Literal["critical", "high", "medium", "low", "nit"]
Verdict = Literal["pass", "concerns", "fail", "unknown"]
Confidence = Literal["low", "medium", "high"]
ConfigMode = Literal["inherit", "scoped", "safe", "bare"]
Access = Literal["toolless", "readonly"]
Scope = Literal["working_tree", "staged", "branch"]
Detail = Literal["summary", "full"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
# Lifecycle states for a background job. Terminal: done|failed|cancelled|timeout.
# (TTL-expired records are deleted and reported as job_not_found, not a state.)
JobState = Literal["running", "done", "failed", "cancelled", "timeout"]
JOB_ID_PATTERN = r"^[0-9a-f]{32}$"
JobId = Annotated[str, Field(pattern=JOB_ID_PATTERN)]
ModelKind = Literal["alias", "full"]
ModelCatalogSource = Literal["static", "none"]


def workspace_warning_for(source: str | None, cwd: str) -> str | None:
    """Warning when the workspace was resolved from the server's own cwd.

    The MCP server process launches from its install directory, so a cwd-resolved
    workspace silently reviews the wrong repo. Surfacing this (rather than failing)
    lets agents notice and pass workspace_root without breaking existing callers.
    Shared by the sync meta builder and the background-job meta rebuild so the two
    paths cannot drift."""
    if source == "cwd":
        return (
            f"workspace resolved from the server's own cwd ({cwd}); pass "
            "workspace_root (or configure an MCP root) to be sure the review "
            "targets the intended repository"
        )
    return None


def branch_range(
    scope: str | None, base: str | None, head: str | None
) -> tuple[str | None, str | None]:
    """Effective (head, diff_range) for a result's meta.

    Only branch scope has a base...head comparison: it reports the effective head
    (defaulting to ``HEAD`` when the caller omitted one) and the ``base...head``
    range string. Non-branch scopes leave both unset. Shared by Meta construction,
    the dry-run result, and the background-job meta rebuild so the derived range
    cannot drift from base+head."""
    if scope != "branch":
        return None, None
    # Coalesce only None (caller omitted head), never "" — an explicit empty
    # string is invalid input that must surface as invalid_head, not be hidden
    # behind a silent HEAD default.
    effective_head = "HEAD" if head is None else head
    return effective_head, f"{base}...{effective_head}"


ErrorCode = Literal[
    "claude_not_found",
    "claude_auth_required",
    "api_key_missing",
    "api_key_invalid",
    "unsupported_config_mode",
    "unsupported_access",
    # A tracked env var (CLAUDE_IN_CODEX_* or ANTHROPIC_API_KEY) arrived as a
    # literal `${...}` placeholder — the MCP host did not expand env substitutions.
    "unexpanded_env_placeholder",
    # An argument failed inputSchema validation before the tool body ran (caught
    # by ValidationEnvelopeMiddleware, so it still returns the ok:false envelope).
    "invalid_arguments",
    "invalid_scope",
    "invalid_base",
    "invalid_head",
    "invalid_paths",
    "invalid_workspace_root",
    "workspace_outside_roots",
    "not_a_git_repo",
    "git_unavailable",
    "context_too_large",
    "timeout",
    "budget_exceeded",
    "claude_permission_error",
    "nonzero_exit",
    "invalid_json",
    "internal_error",
    # The installed `claude` rejected a flag/value this plugin sends — its CLI
    # contract drifted and the plugin likely needs an update.
    "cli_contract_changed",
    # Background-job lifecycle errors (claude_job_result for a non-done job):
    "job_not_found",
    "job_running",
    "job_cancelled",
    "job_timeout",
    "job_failed",
]


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Severity
    title: str
    file: str | None = None
    line: int | None = None
    line_end: int | None = None  # end line when the finding spans a range (line = start)
    evidence: str
    risk: str
    recommendation: str


class RawResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = None
    session_id: str | None = None
    model: str | None = None


class ContextSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cwd: str
    workspace_source: str | None = None  # how cwd was resolved: param|roots|cwd
    workspace_warning: str | None = None  # set when cwd was resolved from server cwd
    config_mode: ConfigMode
    access: Access
    scope: str | None = None
    base: str | None = None
    head: str | None = None
    diff_range: str | None = None  # effective base...head for scope=branch
    paths: list[str] | None = None
    timeout_seconds: int
    elapsed_ms: int
    # The explicit per-call argument. None when the caller omitted it and the
    # configured default was used instead.
    requested_max_budget_usd: float | None = None
    # The raw configured default used when no explicit argument was supplied.
    # It may be outside the supported range and clamped for compatibility.
    configured_max_budget_usd: float | None = None
    # The value actually passed to claude as --max-budget-usd. It is a best-effort
    # stop threshold, not a hard cap; compare against cost_usd for actual spend.
    effective_max_budget_usd: float | None = None
    truncated: bool = False
    truncation_hint: str | None = None
    command_exit_code: int | None = None
    permission_denials: list | None = None
    # Optional `claude` flags this server dropped because the installed CLI did not
    # advertise them in --help (e.g. ["--effort"]). Empty in the common case;
    # informational — guarantee-bearing flags are never dropped, only depth/cosmetic ones.
    compat_warnings: list[str] = Field(default_factory=list)
    # Advisory security posture warnings detected before launching Claude. Example:
    # workspace Claude Code hooks can run outside the tool allowlist unless
    # config_mode=safe/bare disables hooks.
    security_warnings: list[str] = Field(default_factory=list)
    redacted_paths: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    usage: Usage | None = None
    job_id: str | None = None  # set on background-job results; None for sync calls
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    fingerprint: str = FINGERPRINT


class SuccessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    tool: str
    summary: str
    verdict: Verdict
    confidence: Confidence
    findings: list[Finding] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    raw_response: RawResponse = Field(default_factory=RawResponse)
    context_summary: ContextSummary | None = None
    meta: Meta


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ErrorCode
    message: str
    repair: str
    offending_param: str | None = None
    retryable: bool = False
    # Structured repair (house convention, additive; omitted when not mechanical):
    # allowed_values lists the valid values for offending_param; repair_tool plus
    # repair_arguments name a directly callable follow-up.
    allowed_values: list[str] | None = None
    repair_tool: str | None = None
    repair_arguments: dict | None = None


class ErrorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[False] = False
    error: ErrorInfo
    meta: Meta


class ResolvedDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_mode: ConfigMode
    access: Access
    model: str | None = None
    effort: Effort
    max_budget_usd: float
    timeout_seconds: int
    budget_bounds: list[float]  # [min, max] clamp range for max_budget_usd
    timeout_bounds: list[int]  # [min, max] clamp range for timeout_seconds
    practical_min_budget_hint: str


class RawDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_mode: str
    access: str
    model: str | None = None
    effort: str
    max_budget_usd: float
    timeout_seconds: int


class StatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    claude_found: bool
    claude_version: str | None = None
    # Readiness probes (all free — no paid Claude call):
    claude_authenticated: bool | None = None  # None = could not determine
    auth_detail: str | None = None
    version_supported: bool | None = None  # major is in supported_majors()
    # Set when version_supported is False: a major outside the tested range is
    # advisory, not fatal — tools may still work, so we warn instead of blocking.
    version_warning: str | None = None
    # Set when `claude --help` did not list a guarantee-bearing flag this plugin
    # sends — an early, free signal that the CLI contract drifted.
    flags_warning: str | None = None
    # Whether a non-empty ANTHROPIC_API_KEY is present in the environment. Boolean
    # only — the value is never echoed, matching the non-identifying-output posture.
    api_key_present: bool = False
    # Set when a key is present in a login mode (inherit/scoped/safe), where the
    # key is stripped and ignored in favor of OAuth. Advisory, not an error: it
    # explains why a set key has no effect there and is used only in bare.
    api_key_warning: str | None = None
    ready: bool = False  # found AND authenticated AND defaults are usable for paid calls
    readiness_detail: str
    config_modes_available: dict
    hooks_disabled: bool
    raw_defaults: RawDefaults
    resolved_defaults: ResolvedDefaults
    default_errors: list[ErrorInfo] = Field(default_factory=list)
    caveat: str
    fingerprint: str = FINGERPRINT


class ToolCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    cost: Literal["free", "paid"]
    use_when: str
    required_params: list[str] = Field(default_factory=list)
    key_optional_params: list[str] = Field(default_factory=list)
    returns: str


class CapabilitiesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    name: str
    version: str
    fingerprint: str = FINGERPRINT
    fingerprint_covers: list[str] = Field(default_factory=list)
    transport: str
    stability: str
    paid_tools: list[str]
    free_tools: list[str]
    tool_details: list[ToolCapability] = Field(default_factory=list)
    config_modes: list[str]
    access_modes: list[str]
    scope: list[str]  # what this server is for
    negative_scope: list[str]  # what it deliberately does NOT do
    # The complete error-code catalog, published once here instead of being
    # inlined into every tool's output schema (see _error_branch). Required, not
    # defaulted: this is now the catalog's only machine-readable home, so a
    # missing one must fail result construction rather than degrade to [].
    error_codes: list[str]
    # Machine-readable egress disclosure: where paid-tool context goes and the
    # precise limits of redaction. Mirrors the per-tool docstring notes.
    data_egress: str
    prerequisites: list[str]
    deprecation_policy: str
    annotations_policy: str


class JobStarted(BaseModel):
    """Returned by the *_async tools: a handle to poll, not a result."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    job_id: str
    kind: str  # the tool the job runs, e.g. claude_review_changes
    status: JobState = "running"
    started_at: str  # ISO-8601 UTC
    deadline_seconds: int  # wall-clock cap after which a poll reaps the job
    poll_after_ms: int = 1000
    ttl_seconds: int
    expires_at: str | None = None
    meta: Meta
    fingerprint: str = FINGERPRINT


class JobStatus(BaseModel):
    """Returned by claude_job_status: lifecycle state without the full result."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    job_id: str
    kind: str
    status: JobState
    started_at: str
    elapsed_ms: int
    deadline_seconds: int
    poll_after_ms: int = 1000
    ttl_seconds: int
    expires_at: str | None = None
    result_available: bool = False  # true once status == done
    cost_usd: float | None = None  # populated for terminal jobs that spent
    detail: str | None = None  # short human hint (e.g. failure reason)
    fingerprint: str = FINGERPRINT


class DryRunResult(BaseModel):
    """Free preview of what a diff review WOULD send — no Claude call, no spend."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    tool: Literal["claude_review_dry_run"] = "claude_review_dry_run"
    cwd: str
    workspace_source: str | None = None
    workspace_warning: str | None = None
    scope: str
    base: str | None = None
    head: str | None = None
    diff_range: str | None = None  # effective base...head for scope=branch
    paths: list[str] = Field(default_factory=list)
    context_summary: ContextSummary
    diff_bytes: int  # full UTF-8 size of the redacted diff that would be sent
    max_diff_bytes: int  # the server's truncation threshold
    truncated: bool = False  # true when diff_bytes > max_diff_bytes
    truncation_hint: str | None = None
    redacted_paths_count: int = 0
    redacted_paths: list[str] = Field(default_factory=list)
    resolved_config_mode: ConfigMode
    hooks_disabled: bool
    workspace_hook_settings: list[str] = Field(default_factory=list)
    security_warnings: list[str] = Field(default_factory=list)
    fingerprint: str = FINGERPRINT


class JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    kind: str
    status: JobState
    started_at: str
    elapsed_ms: int
    result_available: bool = False
    expires_at: str | None = None
    cost_usd: float | None = None


class JobListResult(BaseModel):
    """Returned by claude_job_list: the workspace's known jobs, newest first."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    jobs: list[JobSummary] = Field(default_factory=list)
    fingerprint: str = FINGERPRINT


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    display_name: str | None = None
    # alias slugs (opus/sonnet/...) track the latest model and are the recommended,
    # stable value; full slugs are pinned exact IDs that go stale each release.
    kind: ModelKind


class ModelCatalogResult(BaseModel):
    """Advisory list of Claude model slugs for the optional `model` param.

    Discovery only: `source` says where it came from and `advisory` states it is not
    authoritative (the `claude` CLI validates the real slug at run time). Returned by
    the claude_models tool and the claude-in-codex://models resource; deliberately NOT embedded
    in claude_capabilities, whose payload is fingerprint-cacheable and must stay stable.
    Claude has no on-disk model cache, so `source` is always "static" (or "none" if the
    bundled list is somehow empty) — there is no live-cache path.
    """

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    source: ModelCatalogSource
    models: list[ModelInfo] = Field(default_factory=list)
    advisory: str
    # Set only when source == "none" (the bundled list is empty).
    unavailable_reason: str | None = None
    fingerprint: str = FINGERPRINT


# Advertised-schema slimming (F1): FastMCP inlines $defs into every tools/list
# entry, so the 25-property Meta model repeats 2-3x per tool and pydantic `title`
# decoration rides along on every field. The slimmed schema is what agents SEE;
# the wire payload still carries the full Meta, whose field contract
# claude_capabilities documents. Measured effect: tools/list 113,495 -> 62,642 bytes.
_META_STUB = {
    "type": "object",
    "description": (
        "Execution metadata: cwd, workspace_source/warning, config_mode, access, "
        "scope, base/head/diff_range, paths, timeout_seconds, elapsed_ms, "
        "requested/configured/effective_max_budget_usd, truncated/truncation_hint, "
        "command_exit_code, "
        "permission_denials, compat/security warnings, redacted_paths, cost_usd, "
        "usage, job_id, request_id, fingerprint. Full contract: claude_capabilities."
    ),
}


# Advertised error-branch slimming (F6/F7): ErrorResult inlines ErrorInfo, whose
# `code` is the 30-value ErrorCode literal, into 11 of 13 tools — the single
# largest repeated block in tools/list. The advertised branch is replaced with an
# open stub that a real ok:false envelope still validates against, so a client
# that validates structured content (MCP states no isError carve-out) stays
# correct. The enum is published once via claude_capabilities.error_codes; the
# wire payload is unchanged and still carries the full typed ErrorInfo.
_ERROR_INFO_STUB = {
    "type": "object",
    "description": (
        "Failure detail: code, message, repair, offending_param, retryable, "
        "allowed_values, repair_tool, repair_arguments. `code` is one of the "
        "error codes published by claude_capabilities (error_codes)."
    ),
}


def _error_branch(meta_schema: dict) -> dict:
    """The advertised ok:false branch, with `meta` supplied by the caller.

    FastMCP dereferences $ref and inlines $defs into each advertised record, so a
    $ref to Meta would cost the same as repeating the stub. Unions whose success
    branches already describe Meta pass the bare open object instead, carrying the
    long description once per tool rather than twice.
    """
    return {
        "type": "object",
        "description": "Error envelope: ok:false with a machine-readable error block.",
        "properties": {
            "ok": {"const": False},
            "error": dict(_ERROR_INFO_STUB),
            "meta": meta_schema,
        },
        "required": ["ok", "error", "meta"],
    }


def _strip_titles(node: object) -> object:
    """Drop pydantic-generated schema `title` decoration.

    Only string-valued `title` keys are decoration; a dict-valued `title` key is a
    real property definition (Finding.title) and MUST survive."""
    if isinstance(node, dict):
        return {
            k: _strip_titles(v)
            for k, v in node.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def _slim(schema: dict) -> dict:
    """Shrink an advertised output schema without changing the wire payload."""
    out = json.loads(json.dumps(schema))  # deep copy; schema is JSON-safe
    defs = out.get("$defs", {})
    if "Meta" in defs:
        defs["Meta"] = dict(_META_STUB)
    # StatusResult.default_errors is list[ErrorInfo], so the catalog rides into
    # STATUS_SCHEMA even though it has no ErrorResult branch of its own.
    if "ErrorInfo" in defs:
        defs["ErrorInfo"] = dict(_ERROR_INFO_STUB)
    return cast("dict", _strip_titles(out))


def _object_union_schema(adapter: TypeAdapter) -> dict:
    """Wrap the success-model union's anyOf in a top-level object schema.

    MCP/FastMCP require an output schema whose top level is ``type: object``;
    a bare ``anyOf`` is rejected. We keep the discriminating ``ok`` key visible
    at the top and carry the full branch schemas (and their $defs) underneath.

    The caller passes only the SUCCESS models; the compact branch built by
    ``_error_branch`` is appended here so every advertised union still accepts an
    ok:false envelope without inlining the error catalog once per tool.
    """
    union = adapter.json_schema()
    branches = union.get("anyOf") or [{k: v for k, v in union.items() if k != "$defs"}]
    # Describe Meta once per advertised record: when a success branch already
    # carries it, the error branch only needs the open object.
    has_meta_def = "Meta" in union.get("$defs", {})
    error_branch = _error_branch({"type": "object"} if has_meta_def else dict(_META_STUB))
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "description": "true = success result, false = error result"},
        },
        "required": ["ok"],
        "anyOf": [*branches, error_branch],
        "$defs": union.get("$defs", {}),
    }


# Advertised output schemas (convention: a discriminated ok:true|false union),
# slimmed for discovery cost — see _slim above.
RESULT_SCHEMA = _slim(_object_union_schema(TypeAdapter(SuccessResult)))
STATUS_SCHEMA = _slim(StatusResult.model_json_schema())
CAPABILITIES_SCHEMA = _slim(CapabilitiesResult.model_json_schema())
# A failed *_async launch returns the error envelope; an empty diff returns a
# SuccessResult without starting a job; an idempotency_key match returns the
# existing job's JobStatus instead of a new JobStarted.
JOB_STARTED_SCHEMA = _slim(
    _object_union_schema(TypeAdapter(JobStarted | JobStatus | SuccessResult))
)
JOB_STATUS_SCHEMA = _slim(_object_union_schema(TypeAdapter(JobStatus)))
# Dry-run and job-list can fail (bad scope/base/workspace), so advertise the union.
DRY_RUN_SCHEMA = _slim(_object_union_schema(TypeAdapter(DryRunResult)))
JOB_LIST_SCHEMA = _slim(_object_union_schema(TypeAdapter(JobListResult)))
MODEL_CATALOG_SCHEMA = _slim(ModelCatalogResult.model_json_schema())
