# Changelog

All notable changes to `claude-in-codex` will be documented in this file.

This project uses pre-1.0 semantic versioning. Minor versions may change the
agent-visible MCP surface; patch versions are reserved for compatible fixes.

## Unreleased

### Added

- Explicit Anthropic data-egress disclosure on the agent-visible surface: each
  paid tool's description now states that context is sent to Anthropic via the
  `claude` CLI and that best-effort secret redaction is applied only to the
  server-gathered diff before it is sent — not to free-form inputs, Claude's
  returned response, or files Claude reads directly under `access=readonly`. A
  machine-readable `data_egress` field was added to `claude_capabilities`, and
  `SECURITY.md` now spells out the same redaction limits. Bumps the contract
  fingerprint to `claude-in-codex/0.1/schema-23`.

### Security

- Hardened diff secret redaction in `context.py` (defense-in-depth on egress to
  Anthropic). Added high-confidence single-token patterns for JWTs, OpenAI
  (`sk-`/`sk-proj-`), Anthropic (`sk-ant-`), Stripe (`sk_live`/`sk_test`), Google
  (`AIza`), GitHub fine-grained (`github_pat_`), GitLab (`glpat-`), npm (`npm_`),
  PyPI (`pypi-`), and connection-string/URI userinfo passwords. Redaction is now
  stateful for multi-line `PRIVATE KEY` / OpenSSH / PGP blocks, dropping the whole
  base64 body (markers stay visible) instead of only the `BEGIN` line. No
  fingerprint change — this is output-redaction behavior, not an MCP-surface
  change.

## 0.6.0 - 2026-06-19

### Changed (breaking)

- Renamed the project, package, Python module (`cc_plugin_codex` →
  `claude_in_codex`), console script (`claude-in-codex-mcp`), MCP server name and
  tool prefix (`mcp__claude-in-codex__*`), and GitHub repo to **claude-in-codex**,
  mirroring the sibling `codex-in-claude`.
- Renamed all environment variables from `CC_PLUGIN_CODEX_*` to
  `CLAUDE_IN_CODEX_*`. **No back-compat aliases** — update your configuration.
- Moved the default background-job cache from `~/.cache/cc-plugin-codex/jobs` to
  `~/.cache/claude-in-codex/jobs`. In-flight jobs created by an older version are
  not discovered unless `CLAUDE_IN_CODEX_STATE_DIR` points at the old path.
- Removed the `cc_codex_capabilities` tool; `claude_capabilities` is now the
  single canonical capabilities tool.
- Bumped the contract `FINGERPRINT` to `claude-in-codex/0.1/schema-22`.

## 0.5.0 - 2026-06-17

- Added `api_key_present` (boolean only — the value is never echoed) and an
  advisory `api_key_warning` to `claude_status`: when `ANTHROPIC_API_KEY` is set
  in a login mode (`inherit`/`scoped`/`safe`), the warning explains that the key
  is stripped and ignored there in favor of OAuth and is used only in
  `config_mode=bare`. The warning does not appear in `bare`, nor for a literal
  `${...}` placeholder (already covered by `unexpanded_env_placeholder`).
- Added an `unexpanded_env_placeholder` diagnostic: `claude_status` now reports
  `ready:false` and names any tracked env var (`CC_PLUGIN_CODEX_*` or
  `ANTHROPIC_API_KEY`) delivered as a literal `${...}` placeholder when the MCP
  host fails to expand env substitutions — including a non-empty placeholder API
  key that would otherwise look valid — and `classify_failure` returns a
  placeholder-aware repair hint on `api_key_invalid`.
- Removed direct Anthropic credential env vars (`ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN`) from Claude subprocess environments for login-backed
  config modes (`inherit`, `scoped`, and `safe`) so stale or placeholder
  credentials cannot override Claude Code OAuth authentication outside
  `config_mode=bare`.
- Added structured `not_a_git_repo` and `git_unavailable` repair errors for
  diff-driven review tools, replacing generic `internal_error` diagnostics for
  common git workspace setup failures.
- Added `readiness_detail` to `claude_status` so `ready:false` reports an
  actionable stop reason, and refined Claude auth/API-key failure
  classification and repair hints by `config_mode`.
- Added optional `paths` filtering to diff-driven review tools so callers can
  review a repo-relative subset of a large diff without leaving the MCP review
  workflow.
- Added structured `invalid_paths` repair errors, filtered diff metadata echo,
  dry-run filter reporting, and truncation hints that name `paths=[...]` as the
  in-tool escape hatch.
- Added an optional `head` ref to the diff-driven review tools
  (`claude_review_changes`, `claude_review_changes_async`,
  `claude_adversarial_review`, `claude_review_dry_run`) so `scope=branch` can
  review `base...head` instead of only `base...HEAD`. `head` defaults to `HEAD`,
  is rejected for non-branch scopes, and resolves locally only — the server
  never fetches refs, calls GitHub, or accepts PR numbers/URLs.
- Added a structured `invalid_head` repair error and reported the effective
  `head` and `diff_range` in result/dry-run/job meta.
- Bumped the agent-visible schema fingerprint to `cc-plugin-codex/0.1/schema-21`.

## 0.4.0 - 2026-06-16

- Passed Claude prompts to the `claude` CLI over stdin instead of argv, avoiding
  process-listing exposure and command-line length limits for large reviews.
- Added structured default-resolution detail to `claude_status`: a `raw_defaults`
  block reporting the unresolved configured defaults and a `default_errors` list
  surfacing per-default resolution failures, so misconfiguration is visible
  before a paid call.
- Aligned MCP config-mode contract metadata, including consistent dry-run error
  metadata for invalid config modes.
- Forwarded the full set of runtime tuning environment variables
  (`CC_PLUGIN_CODEX_*` for git/job/state/input/version knobs) through the bundled
  `.mcp.json` so they take effect when the server is launched from the plugin.
- Updated safe-mode guidance in `SECURITY.md` and the `collaborating-with-claude`
  skill.
- Bumped the agent-visible schema fingerprint to `cc-plugin-codex/0.1/schema-15`.
- Bumped dependencies, including vulnerable transitive packages in `uv.lock`,
  `fastmcp` (3.4.0 → 3.4.2), `ruff`, and `ty`.

## 0.3.1 - 2026-06-16

- Expanded best-effort diff redaction for common credential files and
  password-style keys, including `.netrc`, `.pypirc`, `.envrc`, `password`,
  `passwd`, `pwd`, and `passphrase` patterns.
- Fixed branch-scope diff summaries so `--numstat` is passed before the branch
  revision range.
- Hardened Claude envelope normalization so valid non-object JSON returns a
  structured `invalid_json` error instead of escaping as an exception.
- Classified zero-exit Claude `is_error` and non-success-subtype envelopes with
  the shared failure classifier so budget, auth, permission, rate-limit, API-key,
  and CLI-contract errors get consistent structured codes and retryability.
- Fixed async review startup failures so an unspawnable `claude` command returns
  a structured `claude_not_found` envelope and cleans up partial job records.

## 0.3.0 - 2026-06-16

- Added `config_mode=safe`, backed by Claude Code `--safe-mode`, to disable
  Claude Code customizations and hooks while preserving normal authentication.
- Added compatibility detection for `--safe-mode` so older Claude CLIs report
  `safe` as unavailable and reject `config_mode=safe` locally before a paid call.
- Added an opt-in live integration test for the `config_mode=safe` path that
  skips when the installed Claude CLI does not advertise `--safe-mode`.
- Updated status, dry-run, capabilities, and documentation to describe the new
  safe mode and its hook posture.
- Bumped the agent-visible schema fingerprint to `cc-plugin-codex/0.1/schema-14`.

## 0.2.0 - 2026-06-06

- Added prompt-injection guardrails that tell Claude to treat reviewed diffs,
  evidence, context, and project files as untrusted data.
- Promoted `--no-session-persistence` to a fail-closed Claude CLI contract flag
  so sensitive review sessions are not silently persisted.
- Added advisory detection for workspace Claude Code hook settings and surfaced
  hook posture in status, dry-run, paid result metadata, and background jobs.
- Clarified security documentation: the plugin withholds Bash/write tools, but
  Claude Code hooks can run outside the tool allowlist unless `config_mode=bare`
  is used.
- Bumped the agent-visible schema fingerprint to `cc-plugin-codex/0.1/schema-13`.

## 0.1.4 - 2026-06-06

- Added PyPI-facing package metadata, long description, project links, and
  classifiers.
- Added changelog and security policy documentation.
- Added a Trusted Publishing workflow for tag-triggered PyPI releases.
- Clarified the relationship between the Codex plugin install path and the PyPI
  server package.

## 0.1.3 - 2026-06-05

- Added explicit Claude CLI compatibility documentation and release lockstep
  guidance.
- Exposed structured readiness, review, adversarial review, second-opinion, and
  background job tools for Codex.
- Added local quality gates for linting, formatting, type checking, tests, and
  coverage.
