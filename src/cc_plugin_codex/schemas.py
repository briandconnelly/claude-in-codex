"""Pydantic models for the normalized tool result contract."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# Bump this whenever the agent-visible surface changes: tool names, input or
# output schemas, the ErrorCode set, the config_mode/access/scope/detail value
# sets, or the capability guarantees in CAPABILITY_SUMMARY. Clients cache by it.
FINGERPRINT = "cc-plugin-codex/0.1/schema-15"

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


ErrorCode = Literal[
    "claude_not_found",
    "claude_auth_required",
    "api_key_missing",
    "api_key_invalid",
    "unsupported_config_mode",
    "unsupported_access",
    "invalid_scope",
    "invalid_base",
    "invalid_workspace_root",
    "workspace_outside_roots",
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
    timeout_seconds: int
    elapsed_ms: int
    # The effective (env-defaulted + clamped) value passed to claude as
    # --max-budget-usd. It is a best-effort stop threshold, not a hard cap; compare
    # against cost_usd to see how close actual spend came.
    requested_max_budget_usd: float | None = None
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
    ready: bool = False  # found AND authenticated (version is advisory, not gating)
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
    transport: str
    stability: str
    paid_tools: list[str]
    free_tools: list[str]
    tool_details: list[ToolCapability] = Field(default_factory=list)
    config_modes: list[str]
    access_modes: list[str]
    scope: list[str]  # what this server is for
    negative_scope: list[str]  # what it deliberately does NOT do
    prerequisites: list[str]
    deprecation_policy: str


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


def _object_union_schema(adapter: TypeAdapter) -> dict:
    """Wrap a model union's anyOf in a top-level object schema.

    MCP/FastMCP require an output schema whose top level is ``type: object``;
    a bare ``anyOf`` is rejected. We keep the discriminating ``ok`` key visible
    at the top and carry the full branch schemas (and their $defs) underneath.
    """
    union = adapter.json_schema()
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "description": "true = success result, false = error result"},
        },
        "required": ["ok"],
        "anyOf": union["anyOf"],
        "$defs": union.get("$defs", {}),
    }


# Advertised output schemas (convention: a discriminated ok:true|false union).
RESULT_SCHEMA = _object_union_schema(TypeAdapter(SuccessResult | ErrorResult))
STATUS_SCHEMA = StatusResult.model_json_schema()
CAPABILITIES_SCHEMA = CapabilitiesResult.model_json_schema()
# A failed *_async launch returns the error envelope; an empty diff returns a
# SuccessResult without starting a job.
JOB_STARTED_SCHEMA = _object_union_schema(TypeAdapter(JobStarted | SuccessResult | ErrorResult))
JOB_STATUS_SCHEMA = _object_union_schema(TypeAdapter(JobStatus | ErrorResult))
# Dry-run and job-list can fail (bad scope/base/workspace), so advertise the union.
DRY_RUN_SCHEMA = _object_union_schema(TypeAdapter(DryRunResult | ErrorResult))
JOB_LIST_SCHEMA = _object_union_schema(TypeAdapter(JobListResult | ErrorResult))
