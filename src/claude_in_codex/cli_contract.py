"""Single source of truth for the external `claude` CLI contract.

Every assumption this server makes about the `claude` CLI — its flags,
subcommands, JSON-envelope keys, accepted effort levels, supported major
versions, and the stderr phrasings that mean the contract drifted — lives here so
an upstream breaking change is a one-file, greppable, testable edit. See
COMPATIBILITY.md for the assumption -> upstream-source map.
"""

from __future__ import annotations

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
