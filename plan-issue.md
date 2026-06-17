# Plan: Add `paths` Filtering to Review Tools (#36)

## Summary

Implement #36 as a standalone P1 contract change. Add an optional
`paths: list[str] | None = None` parameter to the four diff-driven tools so
callers can review a subset of a large diff without leaving the MCP review
workflow. Keep the first implementation conservative: plain repo-relative Git
pathspecs only, no Git pathspec magic or exclude syntax.

## Amended Decisions

- Preserve omitted/unfiltered output semantics by representing the effective
  filter as `paths: list[str] | None = None` in shared `Meta` and `JobConfig`.
  Only filtered diff-tool calls should emit `paths`; `claude_ask` and
  unfiltered calls should not gain `paths: []`.
- Use `Field(default_factory=list)` only on result models where `paths` is a
  local, always-meaningful field, such as `DryRunResult`; do not use mutable
  dataclass defaults.
- Make `paths` invalid on `claude_adversarial_review` unless `scope` is also
  provided, because adversarial review only attaches a diff when `scope` is set.
  Return structured `invalid_paths` with `offending_param="paths"`.
- When `paths` is provided but the filtered diff is empty, keep the existing
  no-spend empty-diff short circuit but make it transparent: echo `paths` in
  meta and use a summary that says no changes matched the provided paths.
- Update the `context_too_large` truncation hint to name the new repair path:
  retry with `paths=[...]`, use `scope=staged`, choose a closer branch base, or
  use `claude_ask` with selected context.
- Insert `--numstat` immediately after the `diff` subcommand for summaries.
  This is robust for working tree, staged, branch, `--end-of-options`, and
  `-- <paths...>` ordering.
- Validate `..` segment-wise, not as a substring. Names such as
  `foo..bar/file.py` remain valid.
- Reject all Git pathspec magic in this first version, including `:!`,
  `:(exclude)`, and `:(glob)`. This intentionally defers the issue's suggested
  exclude pathspec support to a follow-up so the first security boundary is
  plain repo-relative paths only.
- Clarify in tool descriptions/capabilities and prompt text that `paths` scopes
  the server-provided diff. With `access=readonly`, Claude may still inspect
  other workspace files directly.
- Golden-envelope regeneration is required along with schema/fingerprint test
  updates because shared output schemas change.

## Key Changes

- Add shared diff options in `context.py`, for example a small `DiffOptions`
  dataclass carrying `scope`, `base`, and normalized `paths`.
- Normalize `paths=None` and `paths=[]` to unfiltered behavior, preserving
  current output when omitted.
- Validate each path before any git subprocess:
  - reject empty strings
  - reject leading `-`
  - reject absolute paths
  - reject `..` path segments
  - reject Git pathspec magic prefixes such as `:!`, `:(exclude)`, `:(glob)`,
    etc.
- Add `InvalidPathsError` and map it to structured `invalid_paths` with
  `offending_param="paths"` and a repair hint that says to pass repo-relative
  pathspecs like `["src", "tests/test_context.py"]`.

## Implementation Changes

- Thread `paths` through `claude_review_changes`,
  `claude_review_changes_async`, `claude_adversarial_review`, and
  `claude_review_dry_run`.
- Build git args as:
  - unfiltered: keep current behavior exactly
  - filtered: append `--` followed by validated paths
  - ensure `--numstat` is inserted before revision/range args and before
    `-- <paths...>` so summaries match the filtered diff.
- Add `paths: list[str] = []` to `Meta`, `DryRunResult`, and `JobConfig`
  persistence, and ensure sync, adversarial, dry-run, async job-start, async
  result, and async error paths echo the effective filter.
- Include effective paths in the Claude prompt for both normal review and
  adversarial attached-diff prompts, so Claude can state the review was scoped.
- Update capabilities metadata so `paths` appears in `key_optional_params` for
  all four diff-driven tools.
- Bump `FINGERPRINT` from `schema-15` to `schema-16`, add `invalid_paths` to
  `ErrorCode`, update the fingerprint digest, and add a `CHANGELOG.md` entry
  under a new unreleased/next-version section following the repo convention.

## Test Plan

- `tests/test_context.py`:
  - filtered working-tree diff includes matching files and excludes nonmatching
    changed files
  - filtered branch diff works
  - summary parity: `ContextSummary.files_changed/lines_*` reflects only
    filtered files
  - `paths=None` and `paths=[]` preserve current behavior
  - reject leading dash, absolute path, `..`, empty path, and pathspec magic
    before git is called
  - large unfiltered diff truncates, while the same diff filtered to a small
    file does not truncate
- `tests/test_server.py`:
  - all four tools accept and pass `paths`
  - `invalid_paths` is returned consistently for all four tools
  - success/error meta and dry-run payload echo `paths`
  - async job start/result meta preserves `paths`
  - capabilities list `paths` for all four tools
- `tests/test_normalize.py`:
  - review and adversarial prompts include the effective `paths` when present
- Contract tests:
  - update schema/fingerprint assertions and `EXPECTED_CONTRACT_DIGEST`
  - verify output schemas remain closed and include `paths`
- Run focused checks first, then the relevant broader gates:
  - `uv run pytest tests/test_context.py tests/test_server.py tests/test_normalize.py tests/test_schemas.py tests/test_fingerprint.py --no-cov`
  - `uv run ruff check src tests`
  - `uv run ruff format --check src tests`
  - `uv run ty check`
  - `uv run pytest -q`

## Assumptions

- #36 is implemented separately from #35; `head` remains out of scope for this
  PR.
- Public parameter name is exactly `paths`.
- First version supports only plain repo-relative pathspecs; exclude pathspecs
  can be a follow-up feature if needed.
- No filesystem glob expansion, workspace traversal, network fetches, or GitHub
  API calls are added.

---

## Review

Verified against the code: file/line references check out, the contract-bump
obligations are right, and the security-first validation framing is correct. The
plan is solid but has one central omission (the motivating discoverability fix)
and under-specifies several edge cases. Recommend closing the gaps below before
executing.

### Most important gaps

1. **Never updates the `context_too_large` truncation hint — the point of #36.**
   The issue exists because a truncated review dead-ends with a hint that says
   "use scope=staged, choose a closer branch base, or call claude_ask"
   (`context.py:204-207`). That hint is how an agent *discovers* the remedy. The
   plan adds `paths` but never adds it to this hint, so the escape hatch stays
   undiscoverable from the failure path. Add `paths=[...]` to the hint text. It is
   a runtime string, not schema surface (no fingerprint impact), but
   `tests/test_context.py` likely asserts on it.

2. **Empty-filtered-diff behavior is unspecified.** Two real cases produce an
   empty diff: `paths` matches files with no changes, or `paths` is a typo that
   matches nothing (git returns empty, not an error). Empty diffs currently
   short-circuit to `ok:true` without spending (`server.py:656`). So a mistyped
   path silently yields a "clean" review with no spend — which reads as "reviewed,
   found nothing" when nothing was reviewed, undercutting the issue's transparency
   requirement. Decide: surface a hint/warning when `paths` was provided but
   `files_changed == 0`. Add a test.

3. **Putting `paths` on the shared `Meta` has a wider blast radius than implied.**
   `Meta` is returned by every tool including `claude_ask` (no diff). With
   `paths: list[str] = []` and `exclude_none` dumping, every envelope — including
   `claude_ask` and all error results — gains `paths: []`, and every golden
   snapshot containing meta changes, not just the four diff tools. Prefer
   `paths: list[str] | None = None` so it is omitted where meaningless and
   populated only on diff tools. The test plan must also explicitly cover
   regenerating `tests/golden/` + `tests/test_golden_envelope.py` (the issue names
   these; the plan's test section does not).

4. **`--numstat` / `--` ordering is the trickiest bit and needs an explicit
   note + test.** Current `_summary` (`context.py:114-119`) *appends* `--numstat`
   when there is no `--end-of-options`. If paths are appended as
   `[..., "--", "src"]`, naive appending puts `--numstat` after `--`, where git
   treats it as a literal pathspec → wrong/zero summary. Safest fix: insert
   `--numstat` immediately after the `diff` subcommand (order-independent) rather
   than patch the append logic. Add a dedicated branch-scope + paths + numstat
   test, since `--end-of-options` + `--numstat` + `--` is the combination most
   likely to break.

### Secondary issues

5. **`access=readonly` weakens the scoping guarantee — unaddressed.** `paths` only
   scopes the server-gathered diff. In `readonly`, Claude reads files directly and
   can consult files outside `paths`. The prompt echo mitigates perception but the
   contract text/prompt should not imply `paths` bounds what Claude sees in
   readonly. Add one clarifying sentence.

6. **`paths` without `scope` on `claude_adversarial_review` is undefined.**
   Adversarial attaches a diff only when `scope` is given (`scope` is optional
   there). Define behavior: ignore `paths` as a no-op, or reject it. Do not leave
   it implicit.

7. **`..` rejection must be segment-based, not substring.** Implement as "split on
   `/`, reject any component equal to `..`". A substring check would wrongly reject
   legitimate filenames like `foo..bar/x`.

8. **Divergence from the issue on exclude pathspecs.** Issue #36 explicitly
   *recommends allowing* `:!`/`:(exclude)` (to drop vendored dirs); the plan
   rejects all magic and defers excludes. That is a defensible conservative call
   (and the Assumptions note it), but the plan should state explicitly that it
   contradicts the issue's recommendation so the reviewer makes a conscious choice.

### Good calls worth keeping

- Validate in `gather_context`/`_diff_args` so all four tools inherit one
  `except InvalidPathsError` clause (matches the existing `InvalidBaseError`
  pattern at `server.py:691`). Make this explicit in the plan.
- The async path builds the prompt at submit time (`server.py:1121` →
  `start_job` stdin), so the worker does not re-gather context — `paths` in
  `JobConfig` is purely meta echo, with no worker-side filtering. The plan's
  persistence-only treatment is correct; state this rationale so a reviewer does
  not expect worker-side filtering.
- `DryRunResult` gaining `paths` is the right enabler: hit `context_too_large` →
  dry-run with `paths` to confirm it now fits → paid review. Tie this loop
  together with gap #1.

### Test-plan additions

Empty/typo'd `paths` → no spend (+ hint); branch-scope numstat ordering;
golden-envelope regeneration; readonly note; adversarial-without-scope behavior;
truncation-hint text now mentions `paths`.
