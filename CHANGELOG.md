# Changelog

All notable changes to `cc-plugin-codex` will be documented in this file.

This project uses pre-1.0 semantic versioning. Minor versions may change the
agent-visible MCP surface; patch versions are reserved for compatible fixes.

## Unreleased

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
- Bumped the agent-visible schema fingerprint to `cc-plugin-codex/0.1/schema-18`.

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
