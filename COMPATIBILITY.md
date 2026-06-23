# Compatibility

`claude-in-codex` is an MCP server that the Codex CLI loads and that shells out to
the Claude Code `claude` CLI.
It therefore depends on two fast-moving upstreams: the `claude` binary (the
delegate it drives) and the Codex host (which loads it).
This document maps every external assumption to where it lives in the code, how a
break is detected, and what to change when it breaks.

Every `claude`-side assumption is centralized in
[`src/claude_in_codex/cli_contract.py`](./src/claude_in_codex/cli_contract.py).
Changing an assumption should be a one-file edit there.

## How the plugin degrades

The design goal is to **fail loudly and safely**, never silently weaken a
guarantee:

- **Guarantee-bearing flags are always sent** (`cli_contract.ALWAYS_SEND_FLAGS`).
  If `claude` removes or renames one, it rejects the invocation at argument
  parsing — *before any model call, so zero spend* — and the failure is reported
  as `cli_contract_changed` with repair guidance.
  These flags carry a security, cost, or behavioral guarantee, so they are **never**
  gated on `--help` parsing (a parsing false-negative must never drop one).
- **Depth/cosmetic flags are feature-detected** (`cli_contract.HELP_GATED_FLAGS`).
  They are dropped (and noted in `meta.compat_warnings`) when the installed
  `claude --help` does not list them, so a minor upstream change degrades instead
  of aborting a paid run.
- **Version is advisory.** A `claude` major outside the tested range warns
  (`claude_status.version_warning`) but does not block; `ready` depends only on the
  CLI being found and authenticated.

## `claude` CLI assumptions

| Assumption | Code (cli_contract constant) | How a break is detected | If it breaks |
| --- | --- | --- | --- |
| Binary on PATH | `CLAUDE_BIN` | `claude_status.claude_found = false` | install Claude Code |
| `-p --output-format json` (core) | `CORE_INVOCATION` | every call errors → `cli_contract_changed`; golden-envelope test | update `CORE_INVOCATION` + `normalize.py` |
| `--no-chrome` (no interactive picker) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` at run time | update the constant |
| `--no-session-persistence` (avoid storing review sessions on disk) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update the constant |
| `--append-system-prompt` (critic guardrails) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update the constant; the prompt is `config.INDEPENDENT_CRITIC_PROMPT` |
| `--max-budget-usd` (spend cap) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update the constant |
| `--tools` (read-only / no-tool guarantee) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update the constant in `config.access_flags` |
| `--strict-mcp-config` / `--mcp-config` (drop user MCP fleet) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update `config.config_mode_flags` |
| `--setting-sources` / `--safe-mode` / `--bare` (mode isolation) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update `config.config_mode_flags` |
| `--effort` + accepted levels | `HELP_GATED_FLAGS`, `VALID_EFFORTS` | dropped with `compat_warnings`; a removed *level* → `cli_contract_changed` | update `VALID_EFFORTS` |
| `--model`, `--disallowed-tools` | `HELP_GATED_FLAGS` | dropped with `compat_warnings` | update the constant |
| JSON envelope keys (`is_error`, `subtype`, `result`, `total_cost_usd`, `usage.*`, `session_id`, `modelUsage`, `permission_denials`) | `ENVELOPE_KEYS`, `USAGE_KEYS`, `SUCCESS_SUBTYPES` | golden-envelope test (`tests/test_golden_envelope.py`); cost silently null if renamed | update `normalize.py` + the golden sample |
| `claude --version` format (`MAJOR.MINOR.PATCH`) | `VERSION_ARGS`, `SUPPORTED_MAJORS` | `claude_status.version_supported = null` | adjust the regex in `config.version_supported` |
| `claude auth status --text` exit code | `AUTH_STATUS_ARGS` | `claude_status.claude_authenticated = null` | update `claude.auth_status` |
| `claude --help` lists long flags | `HELP_ARGS` | preflight fails open (everything assumed supported) | update `preflight._parse_supported` |

⚠️ = **security/cost/behavioral guarantee.** Losing one of these would weaken
read-only access, the no-MCP boundary, the spend cap, or the critic behavior — so
the plugin refuses to run (fails closed) rather than silently proceed.

**Residual risk:** if upstream *accepts but silently no-ops* a guarantee-bearing
flag (rather than rejecting it), the loss cannot be detected without behavioral
testing. The drift detection only catches rejection.

## Detecting drift early (no-spend)

Run-time detection above only fires when a paid call is already in flight. To catch
drift *before* it reaches a user, [`scripts/check_claude_contract.py`](./scripts/check_claude_contract.py)
diffs the installed `claude` CLI against `cli_contract.py` using only the free
local probes (`claude --version`, `claude --help`, and one unknown-flag rejection
probe). No `-p` print run, no model call, no token spend.

```sh
uv run python scripts/check_claude_contract.py
```

It reuses the server's own `preflight._parse_supported` and `config.version_supported`,
and asserts:

- the core invocation (`-p` / `--output-format`) is still present — **exit 1** if gone;
- every `ALWAYS_SEND_FLAGS` guarantee-bearing flag is still listed — **exit 1** if any is missing;
- `HELP_GATED_FLAGS` are present (a miss is a **warning** — the server drops them gracefully);
- the installed major is in `SUPPORTED_MAJORS` (a miss is a **warning** — version is advisory);
- an unknown flag is still rejected with a phrasing that matches
  `CONTRACT_DRIFT_STDERR_PATTERNS` (a miss is a **warning** — upstream may have reworded its error).

Exit codes: `0` holds, `1` drift (a blocker — update `cli_contract.py`), `2` could
not probe (`claude` missing / timed out / help unparseable — nothing verified).

**What it deliberately does NOT check:** the JSON envelope keys, success subtypes,
and usage keys (`ENVELOPE_KEYS` / `SUCCESS_SUBTYPES` / `USAGE_KEYS`) cannot be
observed without a paid `claude -p --output-format json` run, so they are left to
the no-spend golden-envelope fixture test (`tests/test_golden_envelope.py`) and the
manual semantic review. Like the run-time detection, this catches *rejection*, not a
silently no-op'd flag.

CI runs the script as the `Claude CLI contract drift` job in
[`.github/workflows/ci.yml`](./.github/workflows/ci.yml): it installs the latest
published Claude Code as a live canary against the newest CLI, skips gracefully if
the CLI is unavailable, and fails the build on a drift exit.

## Codex-host assumptions

| Assumption | Where | Notes |
| --- | --- | --- |
| Plugin manifest (`mcpServers`, `skills`) | `.codex-plugin/plugin.json` | Codex marketplace schema |
| MCP launch (`uvx --from git+…@tag`) | `.mcp.json` | pinned to a release tag (see below) |
| Entry point `claude-in-codex-mcp` | `pyproject.toml [project.scripts]` | |
| MCP roots as `file://` URIs | `server.py` `_file_roots` | already tolerant; falls back to server cwd |

## Versioning & release (lockstep)

The bundled `.mcp.json` pins the server to a **versioned release tag**
(`vX.Y.Z`) instead of tracking the default branch, so a breaking change no longer
auto-ships to installed users — they update deliberately. The trade-off is that
fixes (including resilience fixes) only reach users on the next tagged release.

When cutting a release, bump these **together**. The `Publish` workflow's
metadata-validation step (`.github/workflows/publish.yml`) hard-checks the
items marked ✅ and aborts the release if any is missing, so omitting one is
caught before publishing:

1. `pyproject.toml` `version` ✅
2. `.codex-plugin/plugin.json` `version` ✅
3. the `@vX.Y.Z` ref in `.mcp.json` ✅
4. `README.md` — the pinned `claude-in-codex==X.Y.Z` install example ✅
5. `CHANGELOG.md` — a new `## X.Y.Z - YYYY-MM-DD` section ✅
6. `FINGERPRINT` in `src/claude_in_codex/schemas.py` — **only** when the
   agent-visible surface changed (tool names, input/output schemas, the
   `ErrorCode` set, the value enums, or the capability summary); not validated
   by the workflow
7. the `plugins/claude-in-codex/` mirror (`.codex-plugin/plugin.json` and
   `.mcp.json`) — keep in sync with the root copies; not validated by the
   workflow

After the release commit is on `main`, check it out (`git switch main &&
git pull`) and publish by tagging it and pushing the tag:

```sh
git tag -a vX.Y.Z -m "claude-in-codex vX.Y.Z"
git push origin vX.Y.Z
```

The tag push triggers the `Publish` workflow, which validates the lockstep
references, runs the test matrix, builds and checks the distributions, publishes
to PyPI, then creates the matching GitHub Release.

The PyPI upload runs in the `pypi` GitHub Actions environment, which requires a
manual approval from a designated reviewer. The run pauses at that gate until the
reviewer approves the deployment (Actions run → **Review deployments** → approve
`pypi`).

The `Publish` workflow also accepts a manual `workflow_dispatch` run with an
explicit version, but the `pypi` environment's deployment-branch policy only
permits `vX.Y.Z` **tag** refs, so a dispatch from `main` is rejected at the
deployment gate and does **not** publish. Push the tag instead.

The `.mcp.json` ref and the git tag must match, or a bundled install fails to
resolve.
