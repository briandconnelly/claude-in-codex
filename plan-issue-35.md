# Plan For Issue #35: Explicit Branch Diff Head

## Context

Issue #35 adds optional `head` ref support to branch-scope review tools so callers
can review `base...head` instead of only `base...HEAD`.

Current repo state:

- Issue #36 (`paths` filtering) is already implemented.
- Current agent-visible fingerprint is `cc-plugin-codex/0.1/schema-16`.
- The diff-driven tools already accept `base` and `paths`, but not `head`.
- The server must not fetch refs, call GitHub, accept PR URLs, or perform network
  work for this feature. Callers are responsible for making refs available
  locally before invoking a review tool.

Claude review note:

- A Claude adversarial review was requested, but local Claude auth/env state
  blocked it.
- MCP review attempts failed with `api_key_invalid`.
- Direct CLI with `ANTHROPIC_API_KEY` unset failed with `Not logged in`.
- This plan therefore reflects local repo review only.

## Implementation Plan

### 1. Context Layer

- Add `InvalidHeadError(ValueError)` in `src/cc_plugin_codex/context.py`.
- Extend `DiffOptions` with `head: str = "HEAD"`.
- Generalize `_base_exists(cwd, base)` to `_ref_exists(cwd, ref)`.
- In `_diff_args`, for `scope="branch"`:
  - validate `base` with `_valid_ref`;
  - validate `head` with `_valid_ref`;
  - emit `git diff --no-ext-diff --no-textconv --end-of-options {base}...{head}`;
  - preserve existing `paths` handling after `--`.
- In `gather_context(cwd, scope, base, paths=None, head=None)`:
  - compute `effective_head = head or "HEAD"`;
  - for branch scope, verify both `base` and `effective_head` resolve to commits;
  - raise `InvalidBaseError` for base failures;
  - raise `InvalidHeadError` for head failures;
  - reject explicit `head` for `working_tree` and `staged`;
  - preserve existing behavior when `head` is omitted.

### 2. Result Contract

- Bump `FINGERPRINT` from `cc-plugin-codex/0.1/schema-16` to
  `cc-plugin-codex/0.1/schema-17`.
- Add `invalid_head` to `ErrorCode`.
- Add these fields to `Meta`:
  - `head: str | None = None`
  - `diff_range: str | None = None`
- Add these fields to `DryRunResult`:
  - `head: str | None = None`
  - `diff_range: str | None = None`
- For branch scope:
  - report effective `head`, including default `HEAD`;
  - report `diff_range` as `{base}...{effective_head}`.
- For non-branch scopes:
  - leave `head` and `diff_range` unset in normal successful results;
  - in rejected calls, error meta may echo the submitted `head` so the offending
    input is visible.
- Add a small internal helper for effective branch range, so `Meta`,
  `DryRunResult`, async persistence, and prompt payloads cannot drift.

### 3. Server Tool API

- Add optional `head: str | None = None` to:
  - `claude_review_changes`
  - `claude_review_changes_async`
  - `claude_adversarial_review`
  - `claude_review_dry_run`
- Field description:
  - head is only valid for `scope="branch"`;
  - default is `HEAD`;
  - value must be a local-resolvable git ref or commit;
  - the server does not fetch refs, call GitHub, or accept PR URLs.
- Thread `head` through:
  - `_resolve`
  - `_resolve_config_mode_only`
  - `_meta` — add `head` as a **keyword-only** arg defaulting to `None`, NOT a new
    positional. There are ~25 positional `_meta(...)` call sites in `server.py`
    that pass `base` positionally; a keyword-only `head` leaves all of them
    untouched and only the branch-scope call sites need to set it.
  - `_execute`
  - `gather_context`
  - `build_prompt` payloads
  - `_empty_diff_result`
  - dry-run result construction
- Reject `head` with structured `invalid_head` before git work when:
  - `scope != "branch"`;
  - `claude_adversarial_review` receives `head` while `scope` is omitted.
- Catch `InvalidHeadError` separately and return:
  - `error.code = "invalid_head"`
  - `error.offending_param = "head"`
  - a repair hint explaining local refs, safe ref syntax, default `HEAD`, and
    branch-scope-only behavior.
- Ensure invalid config/access errors can include submitted `head` in meta
  without triggering Pydantic validation errors.

### 4. Async Jobs

- Add only `head` to `JobConfig` (store inputs, not derived values).
- Persist `head` in `jobs.start_job`.
- In `jobs._build_meta`, restore `head` and **recompute** `diff_range` from
  `base` + effective `head` rather than persisting it separately — keeps the
  derived range from drifting against the stored inputs.
- Ensure `JobStarted.meta` includes effective `head` and `diff_range`.
- Leave `JobStatus` summary-only unless a separate product decision expands its
  contract.

### 5. Capabilities And Docs

- Update `CAPABILITY_SUMMARY` to mention explicit branch head support.
- Update tool descriptions for the four diff-driven tools.
- Add `head` to capability `key_optional_params` for:
  - `claude_review_changes`
  - `claude_review_changes_async`
  - `claude_adversarial_review`
  - `claude_review_dry_run`
- State the non-goal clearly in capability text:
  - no network calls;
  - no `git fetch`;
  - no GitHub API;
  - no PR-number or PR-URL handling.
- Add an Unreleased `CHANGELOG.md` entry.
- Do not bump package version unless the release process is explicitly requested.

### 6. Tests

Update `tests/test_context.py`:

- `_diff_args` uses `{base}...{head}` when `head` is explicit.
- Omitted `head` preserves `{base}...HEAD`.
- Malformed head raises `InvalidHeadError`.
- Non-resolving head raises `InvalidHeadError`.
- Explicit local branch and commit heads work.
- `paths` still filter both diff and `--numstat` summary when `head` is set.
- Explicit `head` with non-branch scope is rejected.
- Branch scope with omitted `head` leaves `diff_range` reporting `{base}...HEAD`
  and `head` reporting effective `HEAD` (not `None`).
- Non-branch successful results/dry-runs leave `head` and `diff_range` unset
  (the "stays None" path, distinct from the rejection path).
- `_empty_diff_result` for branch scope with explicit `head` still reports
  `head` and `diff_range`.

Update `tests/test_server.py`:

- Tool schemas expose `head`.
- Capabilities include `head`.
- Sync review threads `head` into gather/prompt/meta.
- Async review threads `head` into gather/prompt/meta/job config.
- Adversarial review threads `head` when a diff is attached.
- Dry-run includes effective `head` and `diff_range`.
- Malformed and non-resolving heads return structured `invalid_head`.
- Non-branch `head` is rejected.
- Adversarial `head` without `scope` is rejected.

Update `tests/test_jobs.py`:

- `JobConfig` persists `head` and `diff_range`.
- Rebuilt job result meta restores `head` and `diff_range`.

Update `tests/test_schemas.py`:

- Fingerprint is `cc-plugin-codex/0.1/schema-17`.
- `Meta` carries `head` and `diff_range`.

Update `tests/test_fingerprint.py`:

- Update the expected contract digest after intentional schema/API changes.

Update golden-envelope tests:

- Refresh `tests/golden/claude_envelope.json` and normalized expectations only
  where the new meta shape requires it.

### 7. Verification

Run focused tests:

```sh
uv run pytest tests/test_context.py tests/test_server.py tests/test_jobs.py tests/test_schemas.py tests/test_fingerprint.py tests/test_golden_envelope.py --no-cov
```

Run broader gates before PR:

```sh
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest -q
```

## Acceptance Criteria

- `claude_review_changes(scope="branch", base=B, head=H)` reviews `B...H`.
- Omitted `head` preserves current `B...HEAD` behavior.
- `head` support exists on sync review, async review, adversarial attached diff,
  and dry-run.
- Malformed or non-resolving heads return structured `invalid_head`.
- Explicit `head` on non-branch scope is rejected.
- Adversarial review rejects `head` when no diff scope is attached.
- Dry-run and all result meta report effective `head` and `diff_range`.
- Async job config/result meta persists and restores effective `head` and
  `diff_range`.
- Capabilities and tool schemas expose `head`.
- Fingerprint is bumped and golden/fingerprint tests are updated.
- No network calls or fetch/PR URL behavior are added.
