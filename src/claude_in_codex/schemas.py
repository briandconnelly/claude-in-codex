"""Pydantic models for the normalized tool result contract."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, NamedTuple, cast
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)

from claude_in_codex.config import (
    MAX_SYSTEM_PROMPT_APPEND_BYTES,
    paths_within_bounds,
    ref_within_bounds,
)

# Ceiling on any caller-supplied value this server echoes back to the caller --
# `ErrorDetails.value` and every error `message` that names the offending input.
# It lives here, next to the error contract, rather than in server.py because
# context.py raises the errors whose messages carry those echoes and cannot
# import server.py. One constant, so no echo site can drift into being an
# unbounded function of caller input (#150).
DETAIL_VALUE_MAX_CHARS = 200


def bounded_repr(value: str) -> str:
    """A `repr()` of `value` whose RENDERED length obeys DETAIL_VALUE_MAX_CHARS.

    repr() rather than the raw string: this echo lands in an agent-visible error
    message, and repr() renders control characters and terminal escapes inert
    instead of letting them recolor, reposition, or erase the agent's view of the
    error. It also flattens a lone surrogate to an ASCII `\\udddd`, so the
    envelope's later `backslashreplace` pass has nothing left to expand -- a cap
    applied before that pass would otherwise not be a cap on what ships.

    Two ways to bound it are wrong. Slicing the finished repr can sever an escape
    sequence mid-token and drop the closing quote, emitting a faithful rendering
    of nothing. Slicing the raw value first is faithful but bounds the INPUT, not
    the output: 200 code points of `\\xNN` escapes render an order of magnitude
    longer than the cap the rest of the error contract keeps.

    So: shrink the raw head until its repr fits, then mark it. The marker sits
    outside the quotes, so the quoted part is exactly the repr of the head that
    survived and never a truncated escape.

    repr() is never applied to more than the cap's worth of code points. Rendering
    the whole value first -- even just to ask whether it fits -- would allocate
    several times an oversized input to return ~200 characters, which is the same
    "server work proportional to caller input" this function exists to stop; it
    only moved the amplification from the wire to the heap.

    The marker is therefore decided from whether the head covers the WHOLE value,
    not from the rendered length alone. Both conditions are load-bearing: a value
    shorter than the cap can still render past it once escapes expand, and that
    case must truncate and mark even though the head covered everything."""
    head = value[:DETAIL_VALUE_MAX_CHARS]
    rendered = repr(head)
    if len(value) <= DETAIL_VALUE_MAX_CHARS and len(rendered) <= DETAIL_VALUE_MAX_CHARS:
        return rendered
    # Each step drops one code point, so this terminates at "''" in the worst
    # case; the loop only ever runs over a string already cut to the cap.
    while head and len(repr(head)) > DETAIL_VALUE_MAX_CHARS:
        head = head[:-1]
    return repr(head) + "…"


# Bump this whenever the agent-visible surface changes: tool names, input or
# output schemas, the ErrorCode set, the config_mode/access/scope/detail/effort
# value sets, or the capability guarantees in CAPABILITY_SUMMARY. Clients cache by it.
FINGERPRINT = "claude-in-codex/0.1/schema-49"

# Agent-readable disclosure of what the fingerprint covers. Keep in sync with the
# bump rules in the comment above and the pinned surface in tests/test_fingerprint.py.
FINGERPRINT_COVERS = [
    "initialize serverInfo identity (server name; version tracks releases instead)",
    "tool records (names, descriptions, titles, annotations, input/output schemas)",
    "resource and resource-template records and prompt scaffolds",
    "error-code catalog, per-code conditions, and per-tool error maps",
    "error envelope shape (typed details and the repair action)",
    "async-lifecycle descriptor",
    "config_mode/access/scope/detail/effort value sets",
    "detail-level field density, output bounds, and the truncation contract",
    "caller-supplied system-prompt text (parameter, cap, and the meta fingerprint)",
    "the meta.focus contract (what presence and absence attest)",
    # `meta` is advertised as an opaque stub, so its field names would otherwise
    # reach the digest only through the description that enumerates them. They
    # are digested directly as well (#143); disclosed separately because that
    # coverage holds even if the description is ever hand-written again, which
    # the "tool records ... input/output schemas" entry above does not say.
    "meta field names (digested directly, not only via the advertised meta description)",
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
# What a successful *_async START did, as a value rather than a shape a caller has
# to infer from which fields happen to be present (#80). `started` launched a new
# paid job; `existing_job` replayed the one an idempotency_key already holds;
# `no_changes` found an empty diff and returned a free result without launching.
AsyncStartOutcome = Literal["started", "existing_job", "no_changes"]
# What a caller does next with a start envelope. A required literal rather than a
# nullable `next_tool`, because the capabilities payload is dumped with
# exclude_none: a null next_tool would vanish from the wire and leave "there is
# nothing to poll" expressible only as an ABSENT field -- the field-presence
# inference this whole contract exists to remove.
AsyncStartNextAction = Literal["poll_status", "read_payload"]
# The *_async starters, as a closed set. `tool` is typed against it rather than
# left a bare str so the schema itself refuses the bug #80 reports: an envelope
# that names the SYNC tool for a call the caller made against the _async one.
AsyncStartTool = Literal[
    "claude_review_changes_async",
    "claude_consult_async",
    "claude_adversarial_review_async",
]
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


class BoundedSelectors(NamedTuple):
    """The selector fields of a result's `meta`, each bounded by the input caps.

    `dropped` names the fields withheld, so a caller is never left to infer the
    difference between "not supplied" and "withheld"."""

    base: str | None
    head: str | None
    diff_range: str | None
    paths: list[str] | None
    paths_matched: list[int] | None
    dropped: tuple[str, ...]


def bounded_selectors(
    scope: str | None,
    base: str | None,
    head: str | None,
    paths: list[str] | None,
    paths_matched: list[int] | None = None,
) -> BoundedSelectors:
    """`meta`'s selector echo, with anything past the input caps withheld (#162).

    The caps in config.py are enforced at the input edge, so on a LIVE call this
    withholds nothing: a value big enough to trip it here has already been refused
    with invalid_paths/invalid_base/invalid_head. It exists because `meta` is built
    from the raw arguments BEFORE that refusal -- the rejection envelope would
    otherwise carry the very echo the refusal exists to prevent -- and because
    jobs.py rebuilds `meta` from an on-disk record, which is ordinary local state
    that a pre-cap version wrote, or that anyone can edit.

    Two rules keep a withheld value from misleading a reader:

    * `paths` and `paths_matched` are dropped TOGETHER. #149's contract is that the
      two are aligned index-for-index; a surviving count list beside a withheld path
      list would invite a caller to align it against something that is not there.
    * `diff_range` is suppressed whenever either of its components is withheld. It
      is COMPOSED as `base...head`, so bounding the parts without bounding the
      composition would leave the amplification exactly where it was, and a range
      built from a withheld half would name a comparison nobody requested.

    A withheld head is never replaced by branch_range's `HEAD` default: that default
    reports what the server WILL diff when the caller named no head, and reusing it
    for a refused head would report a comparison the caller did not ask for as
    though they had."""
    dropped: list[str] = []
    if not ref_within_bounds(base):
        base, _ = None, dropped.append("base")
    if not ref_within_bounds(head):
        head, _ = None, dropped.append("head")
    effective_head, diff_range = branch_range(scope, base, head)
    if dropped:
        # Either component withheld: the composition cannot be honest, and a
        # defaulted head must not stand in for a refused one.
        effective_head, diff_range = (None if "head" in dropped else effective_head), None
    if not paths_within_bounds(paths):
        paths, paths_matched = None, None
        dropped.append("paths")
    return BoundedSelectors(base, effective_head, diff_range, paths, paths_matched, tuple(dropped))


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
    # Keyed-launch coordination (the store's idempotency index; shared taxonomy):
    "idempotency_conflict",
    "idempotency_result_unavailable",
    "idempotency_in_progress",
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


# Output bounds (#94). `detail` is a field-density level, and summary is a strict
# subset of full: identical field names and types, never an item or character full
# does not also carry. Both levels are bounded server-side so a nominal summary
# cannot consume an unexpected slice of the caller's context window, and neither
# level can silently drop content — whatever a cap removes is reported in the
# result's `truncation` block. Caps are per-field and measured after redaction, so
# a bound can never re-expose a scrubbed secret.
#
# Sizing: the summary profile holds a worst-case result to roughly 6 KB of
# structured text (~1.5k tokens); the full profile is the bounded fallback for a
# result too large to relay whole, not a second opinion on how much detail to give.
TRUNCATION_MARKER = "…[truncated]"


class OutputBounds(BaseModel):
    """Per-field caps for one `detail` level."""

    model_config = ConfigDict(extra="forbid")
    max_findings: int
    max_list_items: int  # questions / assumptions / next_steps, each
    max_summary_chars: int
    max_finding_title_chars: int
    max_finding_text_chars: int  # evidence / risk / recommendation, each
    max_list_item_chars: int
    max_raw_text_chars: int


OUTPUT_BOUNDS: dict[str, OutputBounds] = {
    "summary": OutputBounds(
        max_findings=10,
        max_list_items=5,
        max_summary_chars=1_200,
        max_finding_title_chars=160,
        max_finding_text_chars=400,
        max_list_item_chars=300,
        # summary omits raw_response.text entirely; the cap is inert there.
        max_raw_text_chars=0,
    ),
    "full": OutputBounds(
        max_findings=100,
        max_list_items=50,
        max_summary_chars=8_000,
        max_finding_title_chars=400,
        max_finding_text_chars=4_000,
        max_list_item_chars=2_000,
        max_raw_text_chars=100_000,
    ),
}


class TruncatedField(BaseModel):
    """One field a `detail`-level cap shortened."""

    model_config = ConfigDict(extra="forbid")
    # Dotted path into this result, e.g. "findings" or "raw_response.text".
    field: str
    unit: Literal["items", "chars"]
    returned: int  # items/characters relayed, excluding the truncation marker
    total: int  # items/characters the model produced before the cap


class Truncation(BaseModel):
    """Set only when a cap dropped content; absent means the result is complete.

    Distinct from `meta.truncated`, which reports truncation of the INPUT diff
    before the call. `fields` names every shortened field with exact counts, and
    `next_step`/`tool`/`arguments` are the callable way to get the rest: for a
    background-job result that is a free re-read via claude_job_result with
    detail="full"; for a sync summary it is the same call re-issued with
    detail="full" (paid, so `arguments` is omitted — the original call is yours to
    rebuild). At detail="full" the caps are the relay ceiling, and the step is to
    narrow scope/paths/focus and run a smaller review."""

    model_config = ConfigDict(extra="forbid")
    detail: Detail  # the level that produced this result
    fields: list[TruncatedField]
    next_step: Literal["call_tool", "retry_with_changes"]
    tool: str | None = None
    # Present only when literally callable as-is (the free job re-read).
    arguments: dict | None = None


class ContextSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0


class SystemPromptAppend(BaseModel):
    """Fingerprint of caller-supplied text folded into the system prompt.

    Echoed so a reader of a result knows it was produced under a non-default
    system prompt, and can tell two runs apart, WITHOUT the text itself being
    replayed into the envelope. The text is never carried here or stored; the
    background-job record persists this fingerprint, not the prose.

    Present on every envelope that reports a Claude run: sync results, the
    async launch acknowledgement, and the meta rebuilt for claude_job_result and
    claude_job_consume_result. On those, absent means the guardrail prompt ran
    alone — unless `security_warnings` reports a malformed on-disk fingerprint,
    in which case the prompt used is unknown. For a background job this attests
    what the server RECORDED: the job record is ordinary local state, not
    tamper-evident, so a process that can edit it can also remove the
    fingerprint (or the whole record). It is NOT a general "no persona
    was supplied" signal on envelopes that
    describe no run — argument errors, an empty-diff pass, and context-too-large
    rejections may omit it whether or not the call carried one, because nothing
    was sent to Claude for it to attest."""

    model_config = ConfigDict(extra="forbid")
    # Constrained so a tampered or hand-written on-disk record cannot be replayed
    # as an audit fingerprint: `jobs._fingerprint_from` relies on validation here
    # to degrade impossible values to an absent attestation.
    # Strict types: lax mode would coerce "7", 7.0, or true into a byte count
    # and a non-str digest, hiding a corrupt record behind a valid-looking value.
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: StrictInt = Field(ge=1, le=MAX_SYSTEM_PROMPT_APPEND_BYTES)

    @classmethod
    def of(cls, text: str) -> SystemPromptAppend:
        raw = text.encode()
        return cls(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


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
    # How many changed files each `paths` entry selected, aligned index-for-index
    # with `paths` above. `paths` alone cannot report this: it echoes the caller's
    # list, so it agrees with their typo (#149).
    #
    # A zero means the pathspec selected no CHANGED files -- nothing more. It does
    # not establish that the review missed anything: the entry may be a typo, or
    # may name a real path with no changes in it, and a diff query over an
    # unchanged path covered it correctly. Entries may also overlap and represent
    # very different amounts of the tree, so the counts describe the filter's
    # shape, not the review's coverage. Read a zero as "look at this entry", not
    # as "this scope was skipped".
    #
    # None has exactly four causes: there was no filter; the list exceeded
    # MAX_PATH_MATCH_PROBES; probing hit MAX_PATH_MATCH_SECONDS; or the envelope
    # was rebuilt from a background-job record written before this field existed.
    # That last one is a real post-upgrade case -- records outlive a release by
    # their TTL -- and it is NOT recomputed at fetch time on purpose: the counts
    # describe the diff as gathered at launch, and the working tree may have moved
    # since. A legacy record is recognizable by `paths` being present and
    # non-empty while this is absent.
    paths_matched: list[int] | None = None
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
    # Set when the caller supplied system_prompt_append. On an envelope that
    # describes a run, None means the guardrail prompt ran alone; envelopes that
    # describe no run (argument errors, empty diff, context too large) may omit
    # it either way — see SystemPromptAppend. The text is fingerprinted, never
    # echoed.
    system_prompt_append: SystemPromptAppend | None = None
    # The `focus` text that narrowed a review, echoed VERBATIM rather than
    # fingerprinted: a consumer must be able to tell a user WHAT the verdict was
    # narrowed to, and a digest cannot say that. Caller-authored and untrusted --
    # bounded by MAX_FOCUS_BYTES and refused at the boundary if it forges the
    # server's framing markers, but never treated as instructions.
    #
    # Present means the run THIS envelope describes was launched under that focus, so
    # any verdict beside it covers that focus only and is not a full-review verdict.
    # Deliberately NOT "the text reached Claude": the async lifecycle envelopes
    # (job_running, job_failed, job_cancelled, job_timeout) carry it too, and a job
    # that failed before its child started never sent anything. Presence bounds the
    # verdict, which is what a consumer needs; it does not attest delivery.
    #
    # Absent means the narrowing was not applied or is not known -- never "this was a
    # full review". Envelopes that describe no run omit it whether or not the call
    # carried one: argument errors (a refused focus is never echoed back), the
    # empty-diff pass, and context-too-large. An empty focus is skipped when the
    # prompt is built, so it is no focus here either. On a rebuilt job meta, a record
    # predating focus persistence or holding a malformed value reports the ambiguity
    # in security_warnings rather than letting the absence read as unfocused.
    #
    # Only an envelope that CARRIES a meta can report it: a successful
    # claude_job_status or claude_job_list payload has none.
    focus: str | None = None
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
    # Set only when a detail-level cap dropped content (#94); absent = complete.
    truncation: Truncation | None = None
    meta: Meta


class ErrorDetails(BaseModel):
    """Typed failure detail, so recovery never requires parsing `message`.

    Every field is optional and populated only where it applies to the code; an
    absent field means "not applicable / not known", never "zero". Field names are
    stable across codes, so an agent branches on presence rather than on prose."""

    model_config = ConfigDict(extra="forbid")
    # The offending argument, as a dotted path into the call's arguments.
    field: str | None = None
    # The rejected value, rendered as a string so the detail block never carries a
    # secret-bearing structure verbatim. Omitted when echoing it adds nothing.
    value: str | None = None
    # Short machine-comparable reason token (e.g. "not_a_member", "expired").
    reason: str | None = None
    # Valid choices for `field` when it is a closed enum.
    allowed_values: list[str] | None = None
    # context_too_large, user-supplied text path: the cap and what was supplied.
    # Also the two byte-valued selector caps (#162): a `paths` entry over
    # MAX_PATH_ENTRY_BYTES, or the list over MAX_PATHS_TOTAL_BYTES in aggregate.
    limit_bytes: int | None = None
    actual_bytes: int | None = None
    # The same pair for a COUNT-valued cap -- currently only `paths` exceeding
    # MAX_PATHS_ENTRIES. Separate names rather than reusing the pair above, which
    # says "bytes": reporting 300 entries as 300 bytes would be a number an agent
    # could act on and be wrong about.
    limit: int | None = None
    actual: int | None = None
    # context_too_large, gathered-diff path: the cap and the redacted diff's size.
    max_diff_bytes: int | None = None
    diff_bytes: int | None = None
    # workspace_outside_roots: the client-supplied MCP roots the workspace must
    # sit under. Empty is impossible here — the code only fires when roots exist.
    allowed_roots: list[str] | None = None


RepairStep = Literal[
    # The identical call may succeed later without any change (pairs with retryable).
    "retry_same_call",
    # Change the named argument(s) and call the same tool again. `arguments`, when
    # present, is the corrected call.
    "retry_with_changes",
    # Call the named tool with the given arguments first, then decide.
    "call_tool",
    # Fix something outside the call (install/auth/config); no argument change helps.
    "fix_environment",
    # Nothing mechanical applies; read `repair` prose.
    "no_automatic_repair",
]


# The next step every error of a given code gets when its call site does not name
# a more specific one. Published verbatim as claude_capabilities.error_catalog so
# the documented default and the emitted default cannot drift; a call site that
# CAN do better (a rebuilt call, a job lookup) passes its own RepairAction.
# Every ErrorCode must appear here — tests/test_schemas.py enforces it.
DEFAULT_NEXT_STEP: dict[str, RepairStep] = {
    # Nothing about the call is wrong; the machine or account is not ready.
    "claude_not_found": "fix_environment",
    "claude_auth_required": "fix_environment",
    "api_key_missing": "fix_environment",
    "api_key_invalid": "fix_environment",
    "unexpanded_env_placeholder": "fix_environment",
    "git_unavailable": "fix_environment",
    "cli_contract_changed": "fix_environment",
    # The environment supplied a bad value; env config, not a call argument.
    "unsupported_config_mode": "fix_environment",
    "unsupported_access": "fix_environment",
    # A different call can succeed; the same one never will.
    "invalid_arguments": "retry_with_changes",
    "invalid_scope": "retry_with_changes",
    "invalid_base": "retry_with_changes",
    "invalid_head": "retry_with_changes",
    "invalid_paths": "retry_with_changes",
    "invalid_workspace_root": "retry_with_changes",
    "workspace_outside_roots": "retry_with_changes",
    "not_a_git_repo": "retry_with_changes",
    "context_too_large": "retry_with_changes",
    # A best-effort stop threshold: replaying it spends again and stops again,
    # so the caller must raise the cap or shrink the request (#82).
    "budget_exceeded": "retry_with_changes",
    "claude_permission_error": "retry_with_changes",
    # Transient by nature.
    "timeout": "retry_same_call",
    # Poll, list, or diagnose rather than re-issuing the failed fetch. A terminal
    # job_failed never becomes ok:true on retry, so it points at claude_status.
    "job_not_found": "call_tool",
    "job_running": "call_tool",
    "job_failed": "call_tool",
    # Opaque: the cause is in the message, and no mechanical step follows.
    "nonzero_exit": "no_automatic_repair",
    "invalid_json": "no_automatic_repair",
    "internal_error": "no_automatic_repair",
    "job_cancelled": "no_automatic_repair",
    "job_timeout": "no_automatic_repair",
    # Keyed-launch coordination: a conflict or consumed result needs a changed
    # call (new key); a coordination race clears itself on retry.
    "idempotency_conflict": "retry_with_changes",
    "idempotency_result_unavailable": "retry_with_changes",
    "idempotency_in_progress": "retry_same_call",
}


class RepairAction(BaseModel):
    """The single deterministic recovery instruction for an error.

    `tool` names a tool registered by this server and `arguments` are literally
    callable — pass them through unchanged. Both are absent for the steps that do
    not name a call (retry_same_call, fix_environment, no_automatic_repair), and
    `arguments` may be absent on retry_with_changes when the corrected call could
    not be reconstructed (see `argument_reconstruction` in claude_capabilities)."""

    model_config = ConfigDict(extra="forbid")
    next_step: RepairStep
    tool: str | None = None
    arguments: dict | None = None


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ErrorCode
    message: str
    # Human/agent-readable prose. Recovery logic should branch on `action` and
    # `details`; this stays for the cases prose explains better.
    repair: str
    # True only when re-issuing the IDENTICAL call may succeed later. It never
    # means "a corrected call may succeed" — that is action.next_step
    # =retry_with_changes, which is always paired with retryable=False.
    retryable: bool = False
    # Advisory delay before a retryable retry. None means "no delay is known",
    # not "retry immediately"; always None when retryable is False.
    retry_after_ms: int | None = None
    details: ErrorDetails | None = None
    # Always present: every error names exactly one next step.
    action: RepairAction | None = None

    @model_validator(mode="after")
    def _default_action(self) -> ErrorInfo:
        """Fill `action` from the code's default when a call site did not name one.

        Keeps the field total (every error has exactly one next step) without
        forcing all ~30 construction sites to restate the obvious. An explicit
        retryable=True wins over the table: the site is asserting that this
        particular failure clears on its own, which is exactly retry_same_call."""
        if self.action is None:
            step: RepairStep = (
                "retry_same_call"
                if self.retryable
                else DEFAULT_NEXT_STEP.get(self.code, "no_automatic_repair")
            )
            self.action = RepairAction(next_step=step)
        return self


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
    # The error codes this tool can report, as a branch map for the caller. A code
    # absent here is not raised by this tool. Normally these arrive in an ok:false
    # envelope; claude_status has no error branch and reports its codes through
    # StatusResult.default_errors instead. Conditions are documented once in
    # CapabilitiesResult.error_catalog rather than repeated per tool.
    error_codes: list[str] = Field(default_factory=list)


class ErrorCodeDoc(BaseModel):
    """One entry in the server-wide error catalog: when a code fires and what to do."""

    model_config = ConfigDict(extra="forbid")
    code: str
    condition: str
    # The default action.next_step for this code, straight from DEFAULT_NEXT_STEP.
    # A specific error may carry a more specific action (e.g. invalid_arguments
    # naming the rebuilt call), so read the error's own action first.
    next_step: RepairStep
    # Whether this code can EVER be retried as-is. False means no instance of it
    # is worth replaying unchanged. True means some are — the individual error's
    # own `retryable` is what says whether this one is.
    ever_retryable: bool
    # ErrorDetails fields this code may populate; which ones appear depends on the
    # failing path. Empty when the code carries no typed detail at all.
    detail_fields: list[str] = Field(default_factory=list)


class AsyncStartRoute(BaseModel):
    """One branch of the *_async start contract, as fields rather than prose.

    COMPATIBILITY.md guarantees async_lifecycle is drivable "without parsing
    prose", so what an agent must DO per outcome is expressed structurally here;
    `note` is a human gloss on the same facts, never the only place one lives."""

    model_config = ConfigDict(extra="forbid")
    outcome: AsyncStartOutcome
    # The start_tools that can return this outcome. no_changes is absent from a
    # starter with no diff to find empty.
    tools: list[str]
    # Whether THIS call started a new paid run (false for a replay and for an
    # empty diff, neither of which spends).
    started_new_job: bool
    # Which fields the branch carries, so a caller knows what it may read.
    carries_job_id: bool
    carries_result: bool
    # True when the payload may ALREADY be in a terminal state, so state_field and
    # result_ready_field must be read before polling. This is the replay trap: a
    # keyed retry sent after the job finished returns a done record.
    may_be_terminal: bool
    # What to do next. Always present.
    next_action: AsyncStartNextAction
    # The tool `next_action` names, when it names one. A convenience pointer, not
    # the discriminator -- read next_action.
    next_tool: str | None
    note: str


class AsyncLifecycle(BaseModel):
    """Structured description of the background-job lifecycle.

    This server predates MCP's native task support, so async work runs through
    named tools. Publishing the surface structurally lets an agent drive it
    without parsing capability prose."""

    model_config = ConfigDict(extra="forbid")
    start_tools: list[str]
    # The discriminator on every ok:true response from a start_tool, and what each
    # of its values means for what the caller should do next (#80). Published here
    # so the three starters do not each pay to advertise the routing prose.
    start_outcome_field: str
    # Typed against the union the starters actually advertise, so the published
    # list cannot drift from the discriminator it documents. Costs nothing to
    # advertise: AsyncLifecycle is substubbed for discovery (_CAPABILITIES_SUBSTUBS).
    start_outcomes: list[AsyncStartOutcome]
    start_outcome_routing: list[AsyncStartRoute]
    status_tool: str
    result_tool: str
    consume_tool: str
    cancel_tool: str
    list_tool: str
    # The argument that identifies a job on every lifecycle tool.
    handle_param: str
    # Fields to branch on while polling.
    poll_delay_field: str
    result_ready_field: str
    state_field: str
    running_states: list[str]
    terminal_states: list[str]
    # States that make the result fetch return an ok:false envelope, not a result.
    nonresult_terminal_codes: list[str]
    notes: list[str] = Field(default_factory=list)


class DetailModes(BaseModel):
    """The `detail` contract for every paid tool, in one machine-readable place."""

    model_config = ConfigDict(extra="forbid")
    levels: list[str]
    default: Detail
    # Fields present at full and absent at summary. Everything else is identical
    # in name and type across the two levels.
    full_only_fields: list[str]
    # Level -> per-field caps. Applied after redaction, so a cap never re-exposes
    # a scrubbed secret.
    bounds: dict[str, OutputBounds]
    truncation_marker: str
    truncation: str


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
    # Per-code conditions, default next step, retryability, and typed detail
    # fields. Required for the same reason as error_codes: this is its only home.
    error_catalog: list[ErrorCodeDoc]
    # How ErrorInfo.action.arguments is built, so an agent knows when a
    # retry_with_changes will and will not carry a literally callable call.
    argument_reconstruction: str
    # The background-job lifecycle, structurally.
    async_lifecycle: AsyncLifecycle
    # The `detail` contract: which fields each level carries, the exact per-field
    # caps, and how truncation is signalled and recovered. Published once here so
    # the four paid tools advertise only a pointer (#94).
    detail_modes: DetailModes
    # Machine-readable egress disclosure: where paid-tool context goes and the
    # precise limits of redaction. Mirrors the per-tool docstring notes.
    data_egress: str
    # What meta.focus does and does not attest. Published here rather than in the
    # meta stub because that description is repeated in every tool's output schema,
    # and tools/list is the discovery hot path (tests/test_discovery_cost.py); this
    # payload is the contract's designated home for the full rule.
    meta_focus: str
    prerequisites: list[str]
    deprecation_policy: str
    annotations_policy: str


class JobStarted(BaseModel):
    """Returned by the *_async tools when a new paid job was launched."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    # The invoked surface and the underlying job kind are DIFFERENT tools and are
    # reported separately (#80): `tool` is the *_async starter the caller called,
    # `kind` is the tool whose envelope claude_job_result will return.
    tool: AsyncStartTool
    # No default: a defaulted Literal is omitted from the JSON Schema `required`
    # array, which would leave the advertised union accepting an outcome-less
    # envelope and put the caller straight back to field-presence inference.
    outcome: Literal["started"]
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


# Structurally the JobStatus the caller would have polled, plus the two fields that
# make the async-start union self-describing (#80). claude_job_status keeps
# returning a bare JobStatus: `outcome` answers "what did this LAUNCH do", which is
# not a question a poll asks. Docstrings on these models are one line each because
# pydantic copies them into the advertised schema, where every client pays for them
# (tests/test_discovery_cost.py) -- the reasoning lives in comments like this one.
class AsyncExistingJob(JobStatus):
    """An *_async start that replayed the job its idempotency_key already held."""

    model_config = ConfigDict(extra="forbid")
    tool: AsyncStartTool  # the *_async surface the caller invoked
    outcome: Literal["existing_job"]  # required and undefaulted -- see JobStarted


# No job was started and nothing was spent, so this is a result, not a handle. It
# subclasses SuccessResult so the answer keeps a result's shape -- verdict, summary,
# findings -- because that is what it is: a complete, free review of an empty diff.
# The inheritance is reuse, NOT wire compatibility: SuccessResult forbids extra
# fields, so a strict validator for that model REJECTS this envelope over `outcome`
# and `kind`. This is its own advertised branch of the async-start union and must be
# handled as one.
#
# `outcome` is what makes it distinguishable from a launch without inspecting field
# presence, and `kind` names the job that was NOT started, so `tool` can name the
# *_async surface the caller actually invoked (#80).
class AsyncNoChangesResult(SuccessResult):
    """A diff-bearing *_async start whose empty diff was answered without spending."""

    model_config = ConfigDict(extra="forbid")
    # Narrowed to the two diff-bearing starters: claude_consult_async has no diff
    # to find empty and so can never produce this branch.
    tool: Literal["claude_review_changes_async", "claude_adversarial_review_async"]
    outcome: Literal["no_changes"]  # required and undefaulted -- see JobStarted
    # The job kind that would have run had the diff been non-empty. Present so the
    # (tool, kind) pair means the same thing on all three branches of the union.
    kind: str


# Per-starter narrowings of the three shared branches above. The bases carry the
# SHAPE and are never advertised directly; these carry the IDENTITY, so each
# tool's outputSchema pins its own surface as a const instead of accepting the
# union of all three (a generic client reading claude_consult_async's schema was
# told `tool` might be "claude_review_changes_async").
#
# `kind` is narrowed only where THIS server authors it -- the started and
# no_changes branches, where it comes from the call site. On the existing_job
# branch it is read back from an on-disk job record and can legitimately be "" if
# that record is partial (jobs._status_dict defaults it), so pinning it to a
# Literal there would turn a degraded-but-answerable replay into a pydantic
# ValidationError that escapes the ok:false contract entirely. It stays `str`,
# and `tool` -- which this server always authors -- carries the identity instead.
class ReviewChangesJobStarted(JobStarted):
    """A new claude_review_changes job."""

    tool: Literal["claude_review_changes_async"]
    kind: Literal["claude_review_changes"]


class ReviewChangesExistingJob(AsyncExistingJob):
    """A replayed claude_review_changes job."""

    tool: Literal["claude_review_changes_async"]


class ReviewChangesNoChanges(AsyncNoChangesResult):
    """An empty diff answered by claude_review_changes_async without spending."""

    tool: Literal["claude_review_changes_async"]
    kind: Literal["claude_review_changes"]


class ConsultJobStarted(JobStarted):
    """A new claude_consult job."""

    tool: Literal["claude_consult_async"]
    kind: Literal["claude_consult"]


class ConsultExistingJob(AsyncExistingJob):
    """A replayed claude_consult job."""

    tool: Literal["claude_consult_async"]


class AdversarialJobStarted(JobStarted):
    """A new claude_adversarial_review job."""

    tool: Literal["claude_adversarial_review_async"]
    kind: Literal["claude_adversarial_review"]


class AdversarialExistingJob(AsyncExistingJob):
    """A replayed claude_adversarial_review job."""

    tool: Literal["claude_adversarial_review_async"]


class AdversarialNoChanges(AsyncNoChangesResult):
    """An empty diff answered by claude_adversarial_review_async without spending."""

    tool: Literal["claude_adversarial_review_async"]
    kind: Literal["claude_adversarial_review"]


# The one place a starter's name maps to the models that render its two job-bearing
# branches, so a new starter cannot advertise per-tool consts and then build the
# wrong envelope. The no_changes model is None for a starter with no diff.
ASYNC_START_MODELS: dict[
    str,
    tuple[type[JobStarted], type[AsyncExistingJob], type[AsyncNoChangesResult] | None],
] = {
    "claude_review_changes_async": (
        ReviewChangesJobStarted,
        ReviewChangesExistingJob,
        ReviewChangesNoChanges,
    ),
    "claude_consult_async": (ConsultJobStarted, ConsultExistingJob, None),
    "claude_adversarial_review_async": (
        AdversarialJobStarted,
        AdversarialExistingJob,
        AdversarialNoChanges,
    ),
}


class DryRunResult(BaseModel):
    """Free preview of what a diff review WOULD send — no Claude call, no spend."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    # The deprecated claude_review_dry_run alias was removed in 0.9.0, so there is
    # one registered name and this echoes it.
    tool: Literal["claude_dry_run"] = "claude_dry_run"
    cwd: str
    workspace_source: str | None = None
    workspace_warning: str | None = None
    scope: str
    base: str | None = None
    head: str | None = None
    diff_range: str | None = None  # effective base...head for scope=branch
    paths: list[str] = Field(default_factory=list)
    # Same contract as Meta.paths_matched -- read that field's comment for what a
    # zero does and does not establish. Reported here because the dry run is the
    # FREE preview: it is the one place a caller can catch `paths=["src", "tets"]`
    # before spending, so the counts belong here at least as much as on the paid
    # envelope that carries them (#155).
    #
    # Absent (never null: this result is dumped with exclude_none) when there was
    # no filter, when the list exceeded MAX_PATH_MATCH_PROBES, or when probing hit
    # MAX_PATH_MATCH_SECONDS. Unlike Meta there is no fourth cause: a dry run is
    # always computed fresh, never rebuilt from a stored job record, so a caller
    # need not tell a legacy envelope apart from a filter-less one here.
    #
    # The one field on this result carrying a `description`: the advertised
    # outputSchema is all a generic MCP client gets, and the names around it
    # (`diff_bytes`, `truncated`) say what they are while a bare integer list
    # does not -- a caller who reads a zero as "this scope was skipped" acts on
    # it, which is precisely the misreading the comment above exists to prevent.
    paths_matched: list[int] | None = Field(
        default=None,
        description=(
            "Changed-file count per `paths` entry, aligned index-for-index with "
            "`paths`. A zero means that entry selected no changed files -- a typo, "
            "or a real path with nothing changed in it -- so read it as 'check "
            "this entry', not 'this scope was skipped'. Entries may overlap; the "
            "counts describe the filter, not the review's coverage. Absent when "
            "there was no filter, the list was too long to probe, or probing "
            "timed out."
        ),
    )
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
# The enumeration is GENERATED from Meta.model_fields, not hand-written (#143).
#
# It used to be prose an author had to remember to edit. That made it the gate on
# whether a new Meta field moved the contract digest -- because `meta` is
# advertised as an opaque object, this sentence is the ONLY part of Meta that
# reaches the digest at all. So the enforcement instrument depended on the same
# manual step it exists to catch, and a field added without touching the sentence
# shipped an unbumped contract behind a green test. Verified before the fix:
# adding a field to Meta left tests/test_fingerprint.py fully green.
#
# Generating it fixes both halves at once. The advertised list cannot drift from
# the model, and any added field necessarily moves the description, and so the
# digest. The names are spelled out rather than compressed ("workspace_source,
# workspace_warning", not "workspace_source/warning"): the compressions saved
# bytes at the cost of being un-checkable and un-greppable, and an enumeration
# whose whole purpose is to let an agent read the field list should be the field
# list. `meta_fields_from_description` is the inverse, so the property is
# testable rather than merely true today.
_META_FIELDS_PREFIX = "Execution metadata. Fields: "
_META_FIELDS_SUFFIX = ". Full contract: claude_capabilities."


def meta_fields_from_description(description: str) -> list[str]:
    """Recover the enumerated field names from an advertised meta description.

    The inverse of how _META_STUB's description is built. Lets a test assert the
    advertised enumeration IS Meta's field list rather than trusting that it was
    generated -- so a future hand-written replacement fails as soon as it
    disagrees with the model."""
    body = description
    if body.startswith(_META_FIELDS_PREFIX):
        body = body[len(_META_FIELDS_PREFIX) :]
    if body.endswith(_META_FIELDS_SUFFIX):
        body = body[: -len(_META_FIELDS_SUFFIX)]
    return [name.strip() for name in body.split(",") if name.strip()]


_META_STUB = {
    "type": "object",
    "description": (_META_FIELDS_PREFIX + ", ".join(Meta.model_fields) + _META_FIELDS_SUFFIX),
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
        "Failure detail: code, message, repair, retryable, retry_after_ms, details "
        "(typed), action{next_step,tool,arguments} (always set — branch on it, not on "
        "prose). Codes, per-code conditions, and per-tool code maps: claude_capabilities."
    ),
}


# Sub-blocks of CapabilitiesResult that the payload itself documents field by
# field. Stubbed in the advertised schema only; the wire payload is unchanged.
_CAPABILITIES_SUBSTUBS = {
    "ErrorCodeDoc": ("One error code: code, condition, next_step, ever_retryable, detail_fields."),
    "AsyncLifecycle": (
        "Background-job lifecycle: start/status/result/consume/cancel/list tool names, "
        "handle_param, poll_delay_field, result_ready_field, state_field, "
        "running/terminal states, nonresult_terminal_codes, notes."
    ),
    "ToolCapability": (
        "One tool: name, cost, use_when, required_params, key_optional_params, "
        "returns, error_codes."
    ),
    "DetailModes": (
        "The `detail` contract: levels, default, full_only_fields, per-level "
        "bounds, truncation_marker, truncation (semantics and recovery)."
    ),
    "OutputBounds": (
        "One level's caps: max_findings, max_list_items, max_summary_chars, "
        "max_finding_title_chars, max_finding_text_chars, max_list_item_chars, "
        "max_raw_text_chars."
    ),
}


# Advertised Truncation slimming (#94): the block rides in every advertised
# result union (7 records), and its own field docs are the same contract
# claude_capabilities.detail_modes publishes once. The wire payload is unchanged.
_TRUNCATION_STUB = {
    "type": "object",
    "description": (
        "Set only when a detail cap dropped content; absent = complete result. "
        "Shape, caps, and recovery: claude_capabilities.detail_modes."
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
    # ErrorInfo's own sub-models ride in wherever ErrorInfo does; the stub above
    # already describes their shape, so leaving the full definitions advertised
    # would pay for the same contract twice.
    for name in ("ErrorDetails", "RepairAction"):
        defs.pop(name, None)
    # AsyncStartRoute only ever appears inside AsyncLifecycle, which is substubbed
    # below; leaving its full definition advertised pays for a block no client can
    # reach from the stub.
    defs.pop("AsyncStartRoute", None)
    if "Truncation" in defs:
        defs["Truncation"] = dict(_TRUNCATION_STUB)
        # TruncatedField only ever appears inside Truncation, which the stub above
        # already describes field by field.
        defs.pop("TruncatedField", None)
    # CapabilitiesResult is self-describing on the wire: its payload names every
    # field of these blocks, so advertising their full definitions to every client
    # that only wants the tool list is pure discovery cost.
    for name, summary in _CAPABILITIES_SUBSTUBS.items():
        if name in defs:
            defs[name] = {"type": "object", "description": summary}
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
# A failed *_async launch returns the error envelope; an idempotency_key match
# returns the existing job instead of a new one. Every ok:true branch carries an
# `outcome` literal, so the caller branches on a VALUE rather than on which fields
# are present (#80). Advertised by a starter that has no diff to find empty, so it
# can never answer with a result — its reachable outcomes are started|existing_job.
# One schema per starter: the branches differ only in which tool/kind consts they
# pin, but that is exactly what makes `tool` usable for correlation.
CONSULT_JOB_START_SCHEMA = _slim(
    _object_union_schema(TypeAdapter(ConsultJobStarted | ConsultExistingJob))
)
# The same, plus the no_changes result an empty diff returns without starting a
# job. Only the diff-bearing starters advertise it: carrying the result branch on a
# starter that cannot produce one costs ~3KB of discovery and lies about the
# shapes the caller must handle.
REVIEW_JOB_START_SCHEMA = _slim(
    _object_union_schema(
        TypeAdapter(ReviewChangesJobStarted | ReviewChangesExistingJob | ReviewChangesNoChanges)
    )
)
ADVERSARIAL_JOB_START_SCHEMA = _slim(
    _object_union_schema(
        TypeAdapter(AdversarialJobStarted | AdversarialExistingJob | AdversarialNoChanges)
    )
)
JOB_STATUS_SCHEMA = _slim(_object_union_schema(TypeAdapter(JobStatus)))
# Dry-run and job-list can fail (bad scope/base/workspace), so advertise the union.
DRY_RUN_SCHEMA = _slim(_object_union_schema(TypeAdapter(DryRunResult)))
JOB_LIST_SCHEMA = _slim(_object_union_schema(TypeAdapter(JobListResult)))
MODEL_CATALOG_SCHEMA = _slim(ModelCatalogResult.model_json_schema())
