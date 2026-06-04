# Compatibility

`cc-plugin-codex` is an MCP server that the Codex CLI loads and that shells out to
the Claude Code `claude` CLI.
It therefore depends on two fast-moving upstreams: the `claude` binary (the
delegate it drives) and the Codex host (which loads it).
This document maps every external assumption to where it lives in the code, how a
break is detected, and what to change when it breaks.

Every `claude`-side assumption is centralized in
[`src/cc_plugin_codex/cli_contract.py`](./src/cc_plugin_codex/cli_contract.py).
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
| `--append-system-prompt` (critic guardrails) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update the constant; the prompt is `config.INDEPENDENT_CRITIC_PROMPT` |
| `--max-budget-usd` (spend cap) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update the constant |
| `--tools` (read-only / no-tool guarantee) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update the constant in `config.access_flags` |
| `--strict-mcp-config` / `--mcp-config` (drop user MCP fleet) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update `config.config_mode_flags` |
| `--setting-sources` / `--bare` (mode isolation) ⚠️ | `ALWAYS_SEND_FLAGS` | `cli_contract_changed` | update `config.config_mode_flags` |
| `--effort` + accepted levels | `HELP_GATED_FLAGS`, `VALID_EFFORTS` | dropped with `compat_warnings`; a removed *level* → `cli_contract_changed` | update `VALID_EFFORTS` |
| `--model`, `--disallowed-tools`, `--no-session-persistence` | `HELP_GATED_FLAGS` | dropped with `compat_warnings` | update the constant |
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

## Codex-host assumptions

| Assumption | Where | Notes |
| --- | --- | --- |
| Plugin manifest (`mcpServers`, `skills`) | `.codex-plugin/plugin.json` | Codex marketplace schema |
| MCP launch (`uvx --from git+…@tag`) | `.mcp.json` | pinned to a release tag (see below) |
| Entry point `cc-plugin-codex-mcp` | `pyproject.toml [project.scripts]` | |
| MCP roots as `file://` URIs | `server.py` `_file_roots` | already tolerant; falls back to server cwd |

## Versioning & release (lockstep)

The bundled `.mcp.json` pins the server to a **plugin-scoped release tag**
(`cc-plugin-codex-vX.Y.Z`) instead of tracking the default branch, so a breaking
change no longer auto-ships to installed users — they update deliberately. The
trade-off is that fixes (including resilience fixes) only reach users on the next
tagged release.

When cutting a release, bump these **together**:

1. `pyproject.toml` `version`
2. `.codex-plugin/plugin.json` `version`
3. `FINGERPRINT` in `src/cc_plugin_codex/schemas.py` — **only** when the
   agent-visible surface changed (tool names, input/output schemas, the
   `ErrorCode` set, the value enums, or the capability summary)
4. the `@cc-plugin-codex-vX.Y.Z` ref in `.mcp.json`
5. create and push the matching `cc-plugin-codex-vX.Y.Z` git tag

The `.mcp.json` ref and the git tag must match, or a bundled install fails to
resolve.
