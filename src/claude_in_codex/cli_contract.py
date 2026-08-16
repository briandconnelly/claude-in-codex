"""Single source of truth for the external `claude` CLI contract.

Every assumption this server makes about the `claude` CLI — its flags,
subcommands, JSON-envelope keys, accepted effort levels, supported major
versions, and the stderr phrasings that mean the contract drifted — lives here so
an upstream breaking change is a one-file, greppable, testable edit. See
COMPATIBILITY.md for the assumption -> upstream-source map.
"""

from __future__ import annotations

import re

from pontifex.backend import contract as _pontifex_contract

CLAUDE_BIN = "claude"

# Core invocation that CANNOT be dropped: -p (print mode) + JSON output. If these
# disappear upstream the server cannot function, so a run must fail loudly rather
# than silently degrade.
CORE_INVOCATION = ("-p", "--output-format", "json")
END_OF_OPTIONS = "--"

# Subcommands / probes (free; no paid call).
VERSION_ARGS = ("--version",)
AUTH_STATUS_ARGS = ("auth", "status", "--text")
HELP_ARGS = ("--help",)

# --- Flag classes (see Item 5 of the resilience plan / COMPATIBILITY.md) --------
# ALWAYS_SEND: guarantee-bearing flags, sent unconditionally and NEVER gated on
# `--help` parsing. If upstream removes/renames one, `claude` rejects it at
# arg-parse BEFORE any model call (zero spend) and classify_failure() labels it
# cli_contract_changed. Gating these on the (inherently fuzzy) --help parse could
# silently drop a security/cost/behavioral guarantee, so we never do. All are long
# flags (the diagnostic in claude_status checks them against parsed --help).
ALWAYS_SEND_FLAGS = frozenset(
    {
        "--output-format",  # core JSON output
        "--no-chrome",  # no interactive picker hanging an unattended run
        "--append-system-prompt",  # the independent-critic guardrails
        "--max-budget-usd",  # best-effort spend stop threshold
        "--no-session-persistence",  # avoid storing sensitive review prompts/results on disk
        "--tools",  # read-only / no-tool guarantee
        "--strict-mcp-config",
        "--mcp-config",  # strip the user's MCP fleet (security boundary)
        "--setting-sources",  # scoped-mode isolation
        "--bare",  # bare-mode isolation
        "--safe-mode",  # OAuth-preserving customization/hook isolation
    }
)

# HELP_GATED: dropping one only reduces depth or relies on a still-present primary
# guard — never a safety/cost regression. The value is whether the flag takes an
# argument (so the gate skips the value token too). These are the ONLY flags gated
# on `claude --help`; a false negative here merely drops a harmless flag.
HELP_GATED_FLAGS = {
    "--effort": True,  # reasoning depth only
    "--model": True,  # falls back to the configured default model
    "--disallowed-tools": True,  # defense-in-depth; --tools is the primary allowlist
}

# Cache TTL for the `claude --help` probe, so a long-lived server re-probes after
# an in-place CLI upgrade instead of trusting a stale snapshot forever.
HELP_CACHE_TTL_SECONDS = 300

# --- Reasoning effort -----------------------------------------------------------
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "xhigh"

# --- Supported `claude` major version(s) ----------------------------------------
# A set (not a single int) so a future major can be added without a code change,
# and overridable via env so a user can opt into an untested major themselves.
SUPPORTED_MAJORS = frozenset({2})
SUPPORTED_MAJORS_ENV = "CLAUDE_IN_CODEX_SUPPORTED_MAJORS"

# --- JSON envelope keys read from `claude -p --output-format json` ---------------
# normalize.py / apply_cost_usage parse these tolerantly with .get(); listing them
# here keeps the consumed surface greppable and gives the golden-envelope test a
# canonical reference.
SUCCESS_SUBTYPES = (None, "success")
ENVELOPE_KEYS = frozenset(
    {
        "is_error",
        "subtype",
        "result",
        "total_cost_usd",
        "usage",
        "session_id",
        "modelUsage",
        "permission_denials",
    }
)
USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
)

# --- Contract-drift stderr signatures -------------------------------------------
# Phrasings a CLI prints when it rejects a flag or value we sent. Matching any
# (case-insensitive) reclassifies an otherwise-generic failure as
# cli_contract_changed, telling the user the plugin needs an update for their CLI
# rather than leaving a confusing nonzero_exit.
CONTRACT_DRIFT_STDERR_PATTERNS = (
    "unknown option",
    "unknown flag",
    "unknown argument",
    "unrecognized option",
    "unrecognized argument",
    "no such option",
    "invalid choice",
    "invalid value",
    "unexpected argument",
)


def is_contract_drift(*texts: str | None) -> bool:
    """Whether any provided text carries a contract-drift signature.

    Used on every failure path (sync classify_failure, the zero-exit is_error
    envelope, and the async job error) so drift is labelled consistently no matter
    where `claude` surfaces it."""
    blob = "\n".join(t for t in texts if t).lower()
    return any(pattern in blob for pattern in CONTRACT_DRIFT_STDERR_PATTERNS)


# --- Advisory model catalog -----------------------------------------------------
# Discovery only: surfaced by the claude_models tool and claude-in-codex://models resource so
# an agent can pick a `model` override. The `claude` CLI is the real authority and
# validates the slug at run time, so an unlisted slug may still work and a listed one
# may be unavailable. These contents are NOT fingerprint-stable (the tool/schema are).
MODEL_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# (slug, display_name, kind). Aliases first — they track the latest model and are the
# stable, maintenance-free, recommended value; full IDs are pinned and go stale per
# release, hence kind="full" so a consumer can prefer aliases.
KNOWN_MODELS: tuple[tuple[str, str, str], ...] = (
    ("opus", "Opus (alias → latest Opus)", "alias"),
    ("sonnet", "Sonnet (alias → latest Sonnet)", "alias"),
    ("haiku", "Haiku (alias → latest Haiku)", "alias"),
    ("fable", "Fable (alias → latest Fable)", "alias"),
    ("claude-opus-4-8", "Opus 4.8", "full"),
    ("claude-sonnet-4-6", "Sonnet 4.6", "full"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5", "full"),
    ("claude-fable-5", "Fable 5", "full"),
)


# --- Shared-library contract (pontifex) -------------------------------------------
# Wire prose that would contradict this contract: cross-bridge contamination
# canaries (this code now shares a library with the Codex and Kimi bridges, so
# wrong-direction vocabulary can ride a backport — exactly how moonbridge shipped
# "kimi exec"), and claims of mechanisms this server refuses or lacks (it never
# edits code, has no delegate tier, and read-only is a tool allowlist, not a
# sandbox).
FORBIDDEN_SURFACE_PHRASES = (
    "codex exec",
    "kimi",
    "moonbridge",
    "read-only sandbox",
    "applies the diff",
)

# The declarative half of this contract, in the shared shape the pontifex
# conformance/honesty kits consume. Values are DERIVED from the constants above —
# tests/test_surface_honesty.py pins the derivations so the two can never drift.
# Behavior (command build, classification) still lives in claude.py; migrating it
# onto the pontifex AgentBackend lifecycle is the planned next step while the
# protocol is provisional.
PONTIFEX_CONTRACT = _pontifex_contract.BackendContract(
    backend_id="claude",
    display_name="Claude",
    bin_name=CLAUDE_BIN,
    env_prefix="CLAUDE_IN_CODEX_",
    exec_argv_prefix=CORE_INVOCATION,
    always_send_flags=tuple(sorted(ALWAYS_SEND_FLAGS)),
    help_gated_flags=tuple(sorted(HELP_GATED_FLAGS)),
    forbidden_surface_phrases=FORBIDDEN_SURFACE_PHRASES,
    # Review-only by design: no delegate, no transfer, no sessions
    # (--no-session-persistence is ALWAYS sent). Cost accounting comes from the
    # JSON envelope, not stream events, so usage_accounting is declared with
    # envelope keys as the markers.
    supported_features=frozenset({"usage_accounting"}),
    readonly_honesty_statement=(
        "access=toolless grants no tools at all; access=readonly grants Read/Grep/Glob, "
        "which lets Claude read files itself — bypassing diff redaction — and Read "
        "accepts absolute paths outside the workspace. Neither is an OS sandbox."
    ),
    implicit_context_disclosure=(
        "What the claude CLI auto-loads depends on config_mode: inherit/scoped read the "
        "workspace's CLAUDE.md and .claude/settings*.json — including hooks, which run "
        "OUTSIDE the tool allowlist (surfaced as security_warnings); safe disables "
        "customizations/hooks while preserving OAuth; bare loads nothing but requires "
        "ANTHROPIC_API_KEY. All modes strip the user's MCP servers."
    ),
    # The schema instruction rides the prompt; parsing is tolerant (see normalize).
    structured_output="prompt_append",
    model_catalog=_pontifex_contract.ModelCatalog(
        strategy="static",
        model_identifier_authority="advisory",
        effort_metadata_authority="advisory",
    ),
    isolation_policy=_pontifex_contract.IsolationPolicy.TOOL_ALLOWLIST,
    needs_orphan_sweep=False,
    # claude rejects a bad --effort at arg-parse (VALID_EFFORTS is also enforced
    # at this server's boundary), so upstream is loud, not silent.
    effort_silently_ignored_upstream=False,
    effort_validation="enumerated",  # VALID_EFFORTS is checked at this server's boundary
    usage_event_markers=tuple(sorted(USAGE_KEYS)),
    failure_signatures=_pontifex_contract.FailureSignatures(
        # Narrow on purpose: a bare "/login" can appear in reviewed content or
        # URLs; these phrasings are the CLI's own (see claude.py::_is_auth_blob).
        auth=(r"(?i)not logged in", r"(?i)please run /login"),
        contract_drift=tuple(f"(?i){re.escape(p)}" for p in CONTRACT_DRIFT_STDERR_PATTERNS),
    ),
)
