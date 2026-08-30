# Changelog

All notable changes to `claude-in-codex` will be documented in this file.

This project uses pre-1.0 semantic versioning. Minor versions may change the
agent-visible MCP surface; patch versions are reserved for compatible fixes.

## Unreleased

- Every paid tool now has a recoverable execution path. `claude_consult` and
  `claude_adversarial_review` could block for up to the full timeout with no way
  to get the answer back: a cancelled call, a dropped connection, or a client
  timeout lost work that had already been paid for, because the result lived only
  in the reply that never arrived. `claude_consult_async` and
  `claude_adversarial_review_async` launch the same calls as background jobs and
  return a `job_id`, so the existing `claude_job_status`/`claude_job_result`/
  `claude_job_consume_result`/`claude_job_cancel`/`claude_job_list` lifecycle
  covers all three paid verbs rather than diff review alone. Bumps the fingerprint
  to `claude-in-codex/0.1/schema-37`: two new tools, and the capability payload's
  `paid_tools`, `tool_details`, `async_lifecycle.start_tools`, scope, egress, and
  annotations prose all move with them.

  `kind` on the job handle names the tool whose envelope `claude_job_result` will
  return (`claude_consult`, `claude_review_changes`, `claude_adversarial_review`),
  not the `*_async` starter that began it, so a background result parses with the
  same code as the blocking one.

  The `*_async` tools take no `timeout_seconds`: a job is bounded by the job
  deadline, and `meta.timeout_seconds` reports that deadline rather than the
  synchronous default.

- `claude_consult_async` advertises `JOB_START_SCHEMA`, not `JOB_STARTED_SCHEMA`.
  A diff-bearing starter answers an empty diff with a `SuccessResult` instead of
  a job handle; a consult has no diff to find empty and can never produce that
  shape. Advertising it anyway would cost ~3KB of every session's discovery
  budget and hand the caller a branch that is dead by construction.

- The `idempotency_key` replay/conflict contract is published once in
  `claude_capabilities.async_lifecycle` instead of being re-advertised in full by
  every `*_async` starter — the treatment `detail` got in #94. The per-tool
  description keeps the rule a caller needs before calling (dedupe is on the key
  AND the effective arguments) and points at the lifecycle for the three
  `idempotency_*` outcomes.

- The discovery-cost budget rises from 66,000 to 80,700 bytes. This is the
  largest raise it has taken, and it is the price of the two tools above: an
  `*_async` entry carries the whole job-handle union on top of its own
  parameters. The two cuts named above held it to +21%. Reverting the deprecated
  aliases in 0.9.0 reclaims ~9.8KB, so that revert target moves from 56,300 to
  69,000.

- `COMPATIBILITY.md` records why the server publishes named job tools rather than
  MCP 2025-11-25 native tasks, though it negotiates that version and FastMCP
  supports them: FastMCP's task store is in-process, so a restart would lose a
  handle to a run that has already spent money, while the `claude_job_*` records
  are on disk and survive one. The target Codex CLI is also not known to drive
  `tasks/*` or to surface tool progress. Native tasks stay additive-later, not
  a replacement.

- `idempotency_key` publishes what its "same effective arguments" rule excludes.
  The digest is taken over `(argv, prompt)`, and `detail` reaches neither: it
  selects how a stored result is rendered, not what Claude is asked or paid to
  do. So a keyed retry that changes only `detail` replays rather than
  conflicting. That is the right behavior and now says so — the record keeps the
  raw envelope, so the replayed job can be re-read at any density through
  `claude_job_result` for free, and treating `detail` as effective would force a
  second PAID run to obtain a rendering that costs nothing. Documented at
  `arg_hash_for`, published in `claude_capabilities.async_lifecycle`, and pinned
  by a test, so it is a contract rather than an accident of what reaches argv.
  Amends the unreleased `claude-in-codex/0.1/schema-37`: the contract digest
  moves, and the published `FINGERPRINT` string is unchanged. (Raised by
  Copilot's review of #129.)

- `CAPABILITY_SUMMARY`, the README, and the shipped skill no longer promise a
  job handle unconditionally. Two claims were wrong in the same way — they
  generalized over `paid_tools`, which still contains the deprecated
  `claude_ask`, and over launches, which return the result itself rather than a
  `job_id` when a diff-bearing `*_async` call finds an empty diff. An agent
  reading either could reach for a `claude_ask_async` that does not exist, or
  poll a handle it was never given. All three now say the aliases have no async
  form and tell the caller to branch on `job_id`. Amends the unreleased
  `claude-in-codex/0.1/schema-37` for the summary text. (Raised by Copilot's
  review of #129.)

- The shipped skill's async guardrail is three rules instead of one bundle:
  prefer `_async`, pass `idempotency_key`, cancel what you abandon. An agent
  checks obligations one at a time. (Raised by Copilot's review of #129.)

- An unwritable job-state directory no longer reports `claude_not_found`. A
  launch raises OSError from two unrelated sources — the `claude` executable and
  the state directory — and the branch matched the bare `FileNotFoundError` /
  `PermissionError` types, so both answered "Install Claude Code and ensure
  `claude` is on PATH". A caller whose state directory was read-only was sent to
  reinstall a CLI that was already there and working, while the very next branch
  already carried the right repair. `jobs` now raises a marked
  `ClaudeExecutableError` (keeping the Popen exception types for callers that
  match on them), so the executable's failures keep `claude_not_found` — now
  distinguishing "not on PATH" from "found but not executable", with chmod as the
  repair for the second — and everything else falls through to the
  state-directory repair. Both sides are tested. (Raised by Copilot's review of
  #129.)

- The `claude_job_*` tools no longer describe themselves as diff-review-only.
  The lifecycle serves three starters now, but its own `tools/list` descriptions
  still said to use `claude_job_status` "after claude_review_changes_async",
  promised `claude_job_result` would return "the claude_review_changes
  envelope", and called every job a review job. A client that reads tool
  descriptions rather than calling `claude_capabilities` — which is most of them
  — was never told the two new starters use the same lifecycle. The descriptions
  and the matching `tool_details` now speak of any `*_async` job, and
  `claude_job_result` promises the envelope of the tool named by the job's
  `kind`. (Raised by Copilot's review of #129.)

- `annotations_policy` and the `async_lifecycle` notes drop their remaining
  universal claims over `paid_tools`, which still contains the deprecated
  `claude_ask`. `annotations_policy` had been missed entirely in the first pass
  at this — the edit was written but silently did not apply — so its paid-tool
  list still ended at `claude_review_changes_async`. `COMPATIBILITY.md`'s
  compatibility-strategy paragraph had the same defect. (Raised by Copilot's
  review of #129.)

- The cross-starter idempotency invariant is tested rather than only asserted in
  a comment. All three starters share one namespace in the store's index, and
  `_IDEMPOTENCY_NAMESPACE` claims that reusing a key across two of them
  conflicts rather than replaying the first tool's job — but the digest carries
  only `(argv, prompt)`, not the job kind, and nothing exercised the claim. A
  cross-tool replay would be this key's worst failure: handing back a paid answer
  to a question the caller never asked. It does conflict; now proven. (Raised by
  Copilot's review of #129.)

- A keyed `*_async` retry can no longer report "no changes" over a job that is
  still running and spending. The empty-diff branch returns before any launch,
  so it never reached the idempotency index and `idempotency_key` was simply
  ignored on that path. The sequence is ordinary — and is precisely what the
  shipped guidance now tells an agent to do: launch with a key, lose the
  connection, commit the change while waiting, retry with the same arguments.
  The retry answered `verdict: pass`, "No changes in scope; skipped Claude
  call", with no `job_id` and no hint that a paid job existed; the job kept
  running and kept spending, recoverable only if the agent independently thought
  to call `claude_job_list`. A key that already holds a job is now honored even
  when the current call would not start one: the launch reports
  `idempotency_conflict` and names that job in `action.arguments`. Conflict is
  not a rule invented for this branch — the digest covers the gathered diff, so a
  retry whose diff merely CHANGED already conflicted; this makes a diff that
  changed to nothing behave the same way instead of reporting success. An empty
  diff with no key, or with a key holding nothing, still skips the spend exactly
  as before. Pre-existing for `claude_review_changes_async`, but this PR doubled
  the surface and added the guidance that walks into it. (Raised by an
  independent review of #129.)

- The `claude_not_found` catalog entry describes both of its causes. The
  executable split earlier in this train gave the code a second meaning, "found
  but not executable", while `error_catalog` still published only "not on PATH"
  — the one place an agent looks up what a code means would have had it tell a
  user to reinstall a CLI that was already installed. (Raised by an independent
  review of #129.)

- `tool_details` gets the same review-only prose pin that `tools/list` already
  had. Both halves were corrected together, but only one was tested, and this
  train has already had a prose edit silently fail to apply. `claude_job_result`
  also now advertises the `detail` parameter it accepts, which the new
  idempotency guidance tells callers to reach for. (Raised by an independent
  review of #129.)

- A `claude` on PATH without its execute bit no longer reports "not found". The
  executable split earlier in this train promised a chmod repair, but
  `shutil.which()` tests `X_OK` and so answers `None` for BOTH "absent" and "on
  PATH but not executable" — and the bare name is what production passes
  (`cli_contract.CLAUDE_BIN`). The `ClaudeExecutableNotRunnable` branch was
  therefore unreachable in every real launch, and the chmod repair could never
  be the one a caller actually saw. The test that "proved" the distinction used
  an absolute path, a shape production never sends: a check that could not have
  failed. `_check_executable` now scans PATH for a readable but non-executable
  candidate before classifying, and the new test uses the bare name with an
  isolated PATH. (Raised by Copilot's review of #129.)

- The empty-diff idempotency check is published as a check, not a guarantee. It
  reads the key; it does not serialize on it, so a peer launch that has gathered
  a non-empty diff but has not yet reserved the key stays invisible and an
  empty-diff caller can still answer "no changes" moments before that peer
  spawns. Locking cannot close this: the race is read-before-write, and no
  atomicity on the read can observe a reservation that does not exist yet. Doing
  so needs the empty-diff outcome to TAKE the reservation, which needs a
  reserve-without-spawn primitive the store does not expose — tracked in #131.
  `claude_capabilities.async_lifecycle` now says so plainly and names
  `claude_job_list` as the authority, so the contract does not claim a guarantee
  it cannot keep. (Raised by Copilot's review of #129.)

- The held-key conflict no longer offers a repair that cannot work. It said
  "pass a new idempotency_key to launch a fresh run", but the diff on that
  branch is still empty, so the same call under a fresh key takes the empty-diff
  shortcut again and launches nothing — verified. A repair a caller can follow
  and get nowhere is worse than none: it costs a round trip and teaches them the
  key is broken. It now points at `claude_job_status` first and says plainly
  that a new key alone will not start a run while the diff is empty, naming
  scope/base as the thing to change. The test follows both routes and asserts
  the dead end is a dead end and the named route reaches a job. (Raised by
  Copilot's review of #129.)

- The discovery-budget rationale names the budgets in force. Two derived-value
  references were left at the pre-#93 numbers when the ceiling was raised —
  `ceil(66,000/4)` and "16,500 == 66,000/4" — because comments are not executed
  and nothing checked them. That is the third prose edit in this train to drift
  or silently fail to apply, so `test_the_budget_derivations_in_this_file_are_not_stale`
  now pins the rationale to the constants it explains, and a future raise that
  updates only the numbers fails until the reasoning moves with them. (Raised by
  Copilot's review of #129.)

- The live integration suite covers the async launch path. Its three existing
  tests call the CLI in-process, so every one of them would have passed
  unchanged no matter what this PR did to `_launch_job` — a green gate over a
  diff it does not cover. `test_consult_async_live_roundtrip` runs a real
  detached `claude` through the store: argv construction, the spawned worker,
  the prompt streaming to its stdin, the child's envelope landing in the record,
  and `claude_job_result` rendering it back, asserting a non-zero `cost_usd` so
  a run that never happened cannot pass.

- The three `*_async` starters share one launcher. Idempotency-outcome mapping,
  launch-failure classification, and the job handle were about to be written
  three times; `_launch_job` owns the launch, and each tool keeps only its own
  argument validation, context gathering, and prompt.

- Git calls no longer inherit `GIT_*` environment variables. `GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE` and their relatives override git's
  repository discovery, so a server launched from a git hook — or from any
  parent that exports them — would read a different repository than the
  workspace it resolved, and would send that repository's diff to a paid
  external API with no error. `_git_env()` now drops every `GIT_*` name; none of
  its calls need inherited git state.

- The test suite scrubs `GIT_*` for the session. The git fixtures build
  throwaway repositories in `tmp_path`, and an inherited `GIT_DIR` redirected
  them at the real repository: fixture files were staged into its index and
  every tracked file showed as deleted. Git hooks export `GIT_DIR`, which is why
  the `pre-push` pytest hook failed against a clean checkout.

- The `invalid_arguments` envelope survives FastMCP 3.4.3 and later. That
  release stopped letting the call adapter's pydantic `ValidationError` escape
  and wraps it in FastMCP's own `ValidationError` instead
  (PrefectHQ/fastmcp#4128), so `ValidationEnvelopeMiddleware` no longer caught
  it and a bad call got prose-only `isError:true` content with no code, repair,
  or `structuredContent`. The middleware now accepts both raise forms, keeping
  the `call[...]` title as the discriminator so a tool body's own model error
  still propagates as an internal bug.

## 0.8.0 - 2026-08-22

- An expired legacy record stops blocking its idempotency key. Refusing an
  unverifiable 0.7 key is only defensible because the window closes, but the
  store reaps TTL-expired records lazily, on a store call, and the legacy check
  returns before `start_job_idempotent` would make one. Resolving the marker
  without refreshing the record left an expired job blocking its key
  indefinitely — a permanent refusal wearing the 24h window's justification.
  The check now refreshes the referenced job first, and a reaped record frees
  the key. (The replaced replay path got this for free by calling
  `jobs.status`; the fail-closed rewrite dropped it.)
- The `claude_review_dry_run` capability entry and the migration changelog entry
  no longer claim the deprecated aliases have identical envelopes without
  qualification. A dry-run envelope echoes the invoked name in `tool`, which is
  deliberate and documented above; `claude_ask` genuinely is envelope-identical
  to `claude_consult`. Amends the unreleased `claude-in-codex/0.1/schema-36`:
  the contract digest moves, and the published `FINGERPRINT` string is
  unchanged.
- A keyed launch whose idempotency index could not be read or written reports
  `internal_error`, not `idempotency_in_progress`. The store's `io_error`
  outcome shared the in-progress branch, so the caller was told a concurrent
  launch was being coordinated and that "the winner's job will be replayed" —
  a cause the failure never established, and one that loops a caller against a
  persistently unwritable state directory. The repair now points at the state
  directory instead. `idempotency_in_progress` keeps its retry-the-same-call
  meaning. No schema change: `internal_error` was already published for this
  tool.
- The `idempotency_key` description separates the two recovery paths it had
  grouped as "coordination states you retry through":
  `idempotency_in_progress` is retried with the SAME call, while
  `idempotency_result_unavailable` is non-retryable and needs a NEW key.
  An agent following the old wording could retry the same call forever. Amends
  the unreleased `claude-in-codex/0.1/schema-36`: the contract digest moves, and
  the published `FINGERPRINT` string is unchanged.
- A legacy 0.7 keyed launch is refused rather than replayed unverified. 0.7
  deduped on the key alone, so its `idem-*.json` markers carry no argument
  digest and cannot prove that a retry matches the job the marker names.
  Replaying one contradicted the `(key, effective arguments)` guarantee this
  release publishes: a caller who changed scope, paths, model, effort, or focus
  would silently receive the earlier job's answer, and pay for a review of
  something else. The key now returns `idempotency_conflict` carrying a
  `claude_job_status` repair action for the job the marker names, so the
  existing run stays readable without a second paid launch. Markers are still
  read and reaped but never written, and the record TTL is 24h, so this window
  closes on its own after an upgrade from 0.7. No schema change: the code was
  already published for this tool.
- A paid answer is no longer stranded when the worker is killed between writing
  its result and publishing it. The worker publishes atomically (tmp + rename
  after the child exits), so a worker killed inside the terminate grace window
  leaves a COMPLETE envelope under `result.json.tmp`. The record stayed
  `cancelled`/`timeout` with `result_available: false`, while `cost_usd`
  recovered the spend from that very file — the caller was billed for an answer
  the API would not hand back. A terminal record whose envelope exists ONLY
  under the unpublished name is now promoted to `done` across
  `claude_job_status`, `claude_job_list`, and `claude_job_result`. The JSON
  parse remains the gate, so a torn write is never promoted, and a record
  carrying a published `result.json` keeps the terminal status the store chose
  for it deliberately. Restores 0.7's "a complete envelope wins races" for the
  one case atomic publishing introduced.
- `ClaudeBackend.finalize` coerces a non-string `result` instead of raising. CLI
  drift putting an object, list, or number there reached `extract_json`'s string
  methods when a schema was set, and otherwise escaped into `ExecResult.answer`,
  which is declared `str`.
- Persisted job stderr is sanitized before it reaches disk. The worker redacted
  each streamed line but did not strip Unicode `Cc` code points first, so a
  credential split by one defeated the patterns and sat in `claude-stderr.log`
  in plaintext while the record's own `stderr_sanitized` said it was clean. The
  agent-visible surface was already safe — `_stderr_tail` sanitizes at read
  time — but the record was not. The strip is per line and runs before
  redaction, so the stateful multi-line private-key pass still masks key blocks.
- A keyed launch resolves the idempotency index before it checks that `claude`
  is executable. The check ran first, so if the CLI left PATH between a launch
  and its retry (an MCP restart with a different environment, an upgrade in
  flight), the retry reported `claude_not_found` instead of replaying the
  running job — recovery failing on a resource replay never uses. Creating a
  job still fails fast, and no longer leaves a job record behind when it does.
- The `idempotency_key` description matches what the key now does. It still told
  agents that the key alone determines the match and that changed arguments
  replay the old job — the 0.7 behavior — while the store returns
  `idempotency_conflict`. Since `tools/list` publishes that text, agents were
  being guided into the error. It now documents `(key, effective arguments)`
  matching and names the conflict and coordination codes. Amends the unreleased
  `claude-in-codex/0.1/schema-36`: the contract digest moves, and the published
  `FINGERPRINT` string is unchanged.
- `ClaudeBackend.finalize` tolerates a non-object `usage` block instead of
  raising `AttributeError`. It is the deliberately tolerant envelope read, and
  `normalize.normalize_envelope` already guarded the same field one level up. A
  cost the envelope reported survives a malformed `usage` alongside it.
- `meta.permission_denials` gets the same control-character stripping the
  `claude_permission_error` message got. The message was sanitized with
  `sanitize_echo_prose` while the tree published to metadata was only passed
  through `redact_tree`, so a credential split by a Unicode `Cc` code point
  defeated the patterns and rode out of the agent-visible envelope essentially
  whole — along with the raw control character. One sanitized tree now serves
  both destinations. Reaches the branch where Claude returns usable output AND
  denied tools; the no-output branch returns an error envelope and never
  populated metadata. No schema change.
- The job worker requires an explicit `--config-mode`. It was optional on the
  theory that an older build's argv had to stay launchable, which is not a real
  case: the store spawns each worker once from argv the same process just built
  and never persists or re-execs a command. Optional meant a missing flag would
  silently fall back to inheriting the environment unchanged — the failure-open
  direction on a credential policy.
- Detached jobs get the same credential environment as synchronous ones. The
  shared job store has no environment channel, so the worker inherited the
  server's environment and passed it to `claude` unchanged: a stale
  `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` in the server process could
  override Claude Code's OAuth/session path on an async run under
  `config_mode=inherit`/`scoped`/`safe`, which is exactly what those modes exist
  to prevent — and what the synchronous path already prevented. The job worker
  now applies `ClaudeBackend.scrub_env` for the launching config mode. `bare`
  still keeps the direct credentials it authenticates with. No schema change.
- Detached jobs run `claude` in the workspace again. The shared job store spawns
  the worker with `cwd=<job record dir>` by design, and the worker did not put
  the child back in the workspace, so `config_mode=inherit`/`scoped` stopped
  loading the workspace `CLAUDE.md` and `.claude/settings*.json`, and
  `access=readonly` relative reads resolved inside the cache directory. The
  synchronous tools were never affected, so the two paths had diverged silently.
- Foreign text echoed into an agent-visible error is sanitized with pontonier
  0.6.0's `sanitize_echo_prose`, which deletes Unicode `Cc` code points before
  redacting. A control character wedged into a secret defeated the redactor's
  patterns, so a credential in `claude`'s stderr or in a structured error's
  `result` text could ride out as plaintext in an error message; terminal
  escapes in the same text reached the agent unaltered. Applies to the
  synchronous failure classifier (`stderr` and the structured `result` field),
  job stderr tails, and the `claude_permission_error` message built from denied
  tool calls. No schema change.
- Keyed launches deduplicate on the real effective arguments — the built argv
  and the prompt — instead of an enumerated subset of the job config. `focus`,
  `model`, and `reasoning_effort` were all outside the digest, so the same key
  with a different focus replayed the earlier answer instead of reporting
  `idempotency_conflict`.
- A terminal job recovers recorded spend from a result the worker had written
  but not yet published, so a run reaped in the terminate grace window no longer
  reports `cost_usd: null` for money already spent. `claude_job_status` and
  `claude_job_result`/`claude_job_consume_result` now agree on this figure — an
  earlier fix landed on the status path only, so the two tools reported
  different costs for the same job. A torn write still cannot be surfaced: the
  JSON parse is the gate.
- `claude_dry_run` reports its own name. Its envelope's `tool` field was fixed to
  the deprecated alias `claude_review_dry_run`; each name now echoes the name the
  caller invoked. Amends the unreleased `claude-in-codex/0.1/schema-36`: the
  contract digest moves, and the published `FINGERPRINT` string is unchanged.
- `backend.ClaudeBackend.scrub_env` scrubs the environment it is given rather
  than returning the process environment. Production behavior is unchanged — the
  only caller passed `os.environ` — but the protocol method no longer ignores
  its argument.

- Every model-bearing run now goes through the pontonier `AgentBackend` adapter:
  the sync tools stage via `ClaudeBackend.prepare()` (shared command builder,
  prompt over stdin, help-gate drops on `PreparedRun.dropped_flags`) and keep
  execution local (`run_claude_async` still owns the kill-tree, cancellation,
  and per-mode environment — equivalent to the adapter's `scrub_env`, which
  re-implements the same policy over its argument rather than delegating to it;
  the equivalence is pinned by `tests/test_backend.py`, not by construction),
  while the async job path uses `prepare()` to obtain argv for the detached
  worker (safe because this backend stages no file artifacts). Wire shapes and
  argv are unchanged.

- The redaction engine is now pontonier's (`pontonier.core.redaction`), retiring
  this bridge's local engine after upstream reached parity-or-better on every
  local behavior (stateful private-key-block handling, the full vendor-pattern
  set — the five shapes this bridge contributed are covered upstream — and a
  streaming line redactor for the job worker). Content-level improvements the
  shared engine brings: `[redacted: possibly partial secret value]` honesty
  markers when a match may have stopped short, quoted/bracketed labelled keys,
  richer connection-string coverage, and a source-file code-reference exemption
  so ordinary code like `token = helper(x)` in a `.py` diff is no longer
  masked (data/config files keep unconditional redaction). No schema change;
  each divergence is pinned by a differential test at this bridge's seam.
- Background jobs now run on the shared pontonier job store (fingerprint
  `claude-in-codex/0.1/schema-36`): lifecycle mechanics — detached spawn,
  worker-lock liveness, deadline reaping, TTL cleanup, count caps,
  cancellation — are pontonier's, while the wire shapes, envelope synthesis,
  prompt-off-disk streaming, and sanitized stderr remain this server's. Legacy
  0.7 records stay readable, cancellable, and TTL-reaped in place (same store
  layout lineage), and legacy `idem-*.json` markers are still read and
  reaped, but no longer written or replayed (see the fail-closed entry above). Keyed launches go through the store's
  idempotency index, which dedupes on (key, effective arguments): identical
  retries replay the existing job; the same key with different arguments is
  now an `idempotency_conflict` instead of 0.7's key-only silent replay, and
  two more coordination codes (`idempotency_result_unavailable`,
  `idempotency_in_progress`) are published for `claude_review_changes_async`.
- Canonical tool verbs (fingerprint `claude-in-codex/0.1/schema-35`; superseded
  by schema-36 above in the same unreleased train):
  `claude_ask` is renamed `claude_consult` and `claude_review_dry_run` is
  renamed `claude_dry_run`, matching the verb set shared across the agent
  bridges. The old names remain registered as deprecated aliases — identical
  parameters and schemas — and identical envelopes too, except that a dry-run
  envelope echoes the invoked name in `tool` (see the entry above) — with the
  deprecation surfaced in tool
  descriptions and `claude_capabilities` — and are planned for removal in
  0.9.0. The `tools/list` discovery budget is temporarily raised for the
  duplicated alias schemas and reverts with the removal.
- Diff redaction preserves the input's trailing newline, so returned diffs are
  `git apply`-able (ports the sibling bridges' fix; end-to-end
  redact-then-apply regression test included).

- Add a dependency on [pontonier](https://github.com/briandconnelly/pontonier),
  the shared agent-bridge library, and declare this server's CLI contract in
  its shared shape: `cli_contract.PONTONIER_CONTRACT` (derivation-pinned
  against the legacy constants) plus `cli_contract.FORBIDDEN_SURFACE_PHRASES`
  with surface-honesty tests enforcing them against the built wire surface.
- Add `backend.ClaudeBackend`, this bridge's adapter on the frozen pontonier
  `AgentBackend` protocol (`contract_api_version = 1`), validated by a
  byte-for-byte argv differential against `claude.build_command` and
  per-config-mode env scrubbing tests. Every model-bearing run is staged through
  it. The adapter itself adds no agent-visible surface; the fingerprint moves for
  the tool renames and the idempotency error codes recorded above.

### Fixed

- MCP initialization reported FastMCP's version as the server version, so
  `initialize.serverInfo` announced e.g. `claude-in-codex/3.4.2` while
  `claude_capabilities` reported the application version `0.7.0`. Hosts use
  initialize metadata for diagnostics, caching, and compatibility decisions, so a
  framework upgrade looked like an application release and an actual release was
  invisible without spending an extra tool call. `serverInfo.version` is now
  `claude_in_codex.__version__` — the same source `claude_capabilities` reports —
  and the contract-fingerprint surface now pins the serverInfo identity (its
  release-tracking `version` excluded, so the fingerprint does not churn per
  release). Bumps the contract fingerprint to `claude-in-codex/0.1/schema-34`.

### Changed

- `detail` is now a documented, bounded field-density level rather than an
  undescribed `summary|full` switch. `summary` was already dropping
  `raw_response.text` and `context_summary`, but it still relayed every finding,
  question, assumption, and next step at whatever length the model produced, so a
  nominal summary could consume an unbounded slice of the caller's context. Both
  levels are now capped server-side per field (`summary`: 10 findings, 5 items per
  list, 1,200-character summary; `full` raises each cap and adds a 100,000-character
  ceiling on `raw_response.text`), and `summary` is a strict subset of `full` —
  identical field names and types, never an item `full` does not also carry.
  Nothing is dropped silently: a capped result carries a new `truncation` block
  naming each shortened field with exact `returned`/`total` counts, a
  `…[truncated]` marker on shortened strings, and a callable next step. `findings`
  are now ordered most-severe-first at both levels, so an item cap drops the least
  severe finding rather than an arbitrary one. Model-derived finding paths and
  `meta.permission_denials` are bounded too, so no model-supplied field escapes the
  caps. `claude_job_result` and `claude_job_consume_result` accept `detail`, which
  re-renders the *stored* envelope — so a truncated background-job summary is
  recoverable at full detail for free, and that call is what a job result's
  `truncation.arguments` hands back ready to run, workspace pinned so it is callable
  as-is. Because deletion is irreversible, `claude_job_consume_result` renders at
  full detail unless an explicit `detail` is passed, and a consumed result's
  truncation block never names the record it just destroyed. Subsetting is scoped
  precisely: it covers content items and characters, deliberately excluding the
  `truncation` block itself and the truncation marker, which are metadata about the
  bounding. The complete contract (per-level caps, truncation semantics,
  recovery) is published once as `claude_capabilities.detail_modes`; the paid tools
  advertise only a pointer, holding discovery cost to 52,584 -> 55,367 `tools/list`
  bytes (+5.3%). Bumps the contract fingerprint to `claude-in-codex/0.1/schema-33`
  (#94).

- Failure recovery is now machine-actionable without parsing prose. `ErrorInfo`'s four
  scattered recovery fields (`offending_param`, `allowed_values`, `repair_tool`,
  `repair_arguments`) are replaced by two typed blocks: `details` (`field`, `value`,
  `reason`, `allowed_values`, `limit_bytes`/`actual_bytes`, `max_diff_bytes`/`diff_bytes`,
  `allowed_roots`) and `action` — always present, naming exactly one `next_step` of
  `retry_same_call`, `retry_with_changes`, `call_tool`, `fix_environment`, or
  `no_automatic_repair`, plus a registered `tool` and literally callable `arguments` where
  one applies. `retryable` now means only "the identical call may succeed later" and is
  paired with a nullable `retry_after_ms`; a call that needs different arguments is
  `retryable: false` with `next_step: retry_with_changes`. Repairs are callable on the
  first attempt: an `invalid_arguments` failure returns the original call with only the
  invalid argument removed (omitted above 8 KiB so a large prompt is never echoed back),
  `job_not_found` and `job_running` pin the resolved `workspace_root`, `context_too_large`
  reports both the cap and the actual size, and `workspace_outside_roots` publishes the
  client's roots. `claude_capabilities` gains `error_catalog` (per-code condition, default
  next step, whether the code is ever retryable as-is, and the typed detail fields it may
  populate — the next step is derived from the same table the envelope uses, so the
  documented and emitted defaults cannot drift), `argument_reconstruction`, a structured
  `async_lifecycle` descriptor for the background-job tools, and a per-tool `error_codes`
  branch map on every `tool_details` entry. Discovery cost is held roughly flat — 51,855 ->
  52,579 `tools/list` bytes (+1.4%) — by stubbing the new capability sub-models out of the
  advertised schemas. Bumps the contract fingerprint to `claude-in-codex/0.1/schema-32`
  (#60).

- Cut the `tools/list` discovery cost from 63,970 bytes / 15,381 tokens to 51,855 bytes /
  12,570 tokens (-19%), the per-session tax every preloading client pays before its first
  useful call. The 30-value error-code catalog was inlined into 11 of 13 output schemas via
  the `ErrorResult` branch (and again into `claude_status` through
  `StatusResult.default_errors`); advertised schemas now carry a compact `ok:false` branch
  instead. The branch stays *conforming* — a real error envelope still validates against
  every advertised schema — so clients that validate structured content are unaffected, and
  the wire payload is byte-for-byte unchanged with its full typed `ErrorInfo`. The catalog is
  published once as `claude_capabilities.error_codes`. Bumps the contract fingerprint to
  `claude-in-codex/0.1/schema-31` (#90).

### Fixed


- `budget_exceeded` errors now set `retryable: false` because replaying the same paid
  call with the same cap cannot resolve the stop condition. Repair guidance names the
  caller-controlled changes that can help: raise `max_budget_usd` or narrow the supplied
  prompt/context. Bumps the contract fingerprint to `claude-in-codex/0.1/schema-30` (#82).

- Explicit per-call `max_budget_usd` values outside `$0.01-$5.00` are now rejected before
  Claude can launch instead of being silently clamped. Paid-tool input schemas publish both
  bounds, while result metadata distinguishes the raw requested/configured value from the
  effective value used after compatibility clamping of environment defaults (#92). Bumps the
  contract fingerprint to `claude-in-codex/0.1/schema-29`.

- Paid-tool annotations and descriptions now disclose that workspace Claude Code hooks can
  run arbitrary shell in `config_mode=inherit`/`scoped`: paid tools are `destructiveHint:true`
  because static annotations must represent the worst-case config mode, and
  `annotations_policy` names `safe`/`bare` as the hook-disabled modes (#91). Bumps the
  contract fingerprint to `claude-in-codex/0.1/schema-28`.

- `job_failed` errors no longer set `retryable: true`. The job record is terminal, so
  re-fetching the same `job_id` returns `job_failed` forever; under the tightened retry
  semantics that flag would loop an agent on a call that can never succeed. The error now
  points at the free `claude_status` readiness probe, which is the one mechanical step that
  separates a broken install or login from a one-off run failure (#60).

## 0.7.0 - 2026-08-03

### Added

- Agent-friendliness remediation (fingerprint `claude-in-codex/0.1/schema-27`):
  - Argument-validation failures now return the standard `ok:false` envelope
    (new error code `invalid_arguments`) instead of prose-only text; the
    capability summary names the error carrier.
  - Structured repair fields on the error envelope: `allowed_values`,
    `repair_tool`, `repair_arguments` (additive; omitted when not mechanical).
  - `idempotency_key` on `claude_review_changes_async`: a duplicate launch
    within the job TTL returns the existing job instead of spending again
    (atomic per-workspace on-disk reservation; same-key launches cannot
    double-spawn on a shared local filesystem).
  - Canonical `claude-in-codex://models` resource URI; `claude://models`
    remains as a deprecated alias for a compatibility window.
  - `fingerprint_covers` and `annotations_policy` fields on
    `claude_capabilities`; the pinned contract digest now covers full tool and
    resource records (descriptions, titles, annotations), resource templates,
    and prompts, and `fingerprint_covers` states that coverage.
  - Honest tool annotations: paid tools and job status/result/list polls are
    advertised `readOnlyHint:false` (spend/egress and lazy job maintenance are
    observable effects); `claude_job_consume_result` is `destructiveHint:true`;
    `claude_job_cancel` is `idempotentHint:true`; a new `annotations_policy`
    field on `claude_capabilities` states the policy.
  - Advertised output schemas slimmed (Meta stubbed, pydantic titles stripped):
    `tools/list` wire size roughly halved (113,495 → 62,642 bytes); a new
    `tests/test_discovery_cost.py` ratchets the budget.

- `claude_models` tool and `claude://models` resource: an advisory, free,
  read-only catalog of the model slugs accepted by the `model` parameter
  (aliases such as `opus`/`sonnet` plus pinned full IDs, each tagged `kind`).
  Bundled-static; the `claude` CLI remains the run-time authority. Bumps the
  contract fingerprint to `claude-in-codex/0.1/schema-25`.

- Explicit Anthropic data-egress disclosure on the agent-visible surface: each
  paid tool's description now states that context is sent to Anthropic via the
  `claude` CLI and discloses what best-effort secret redaction does and does not
  cover. A machine-readable `data_egress` field was added to
  `claude_capabilities`, and `SECURITY.md` spells out the same limits. (Redaction
  coverage was extended to Claude's returned output in #66 — see the Security
  entry below for the current scope; free-form inputs and `access=readonly`
  direct reads remain uncovered.) Part of the contract fingerprint bump to
  `claude-in-codex/0.1/schema-24`.

### Security

- Hardened background-job persistence and lifecycle handling: stderr is now
  redacted before storage or error-envelope inclusion, job identifiers are
  canonical lowercase hexadecimal values confined to the workspace state
  directory, and persisted processes must prove ownership with a worker-held
  advisory lock before lifecycle operations can signal them. Legacy raw stderr
  logs are withheld. Bumps the contract fingerprint to
  `claude-in-codex/0.1/schema-27` because the job-id input schemas are narrowed.
- Extended best-effort secret redaction to **Claude's returned output**, closing
  the egress gap the disclosure previously only admitted (#66). A shared
  `redact_text` helper in `context.py` (reusing the diff path's pattern set and
  stateful PEM/OpenSSH/PGP key-block logic) now scrubs every model-derived field
  relayed to the caller: structured `summary`/`findings`/`questions`/`assumptions`/
  `next_steps`, the `detail=full` raw response text, and model-derived error
  messages. Redaction runs after string coercion so secrets hidden in nested
  object keys are caught. The `data_egress` field, paid-tool descriptions, and
  `SECURITY.md` are updated to state the new coverage; free-form caller inputs
  are still sent verbatim. Bumps the contract fingerprint to
  `claude-in-codex/0.1/schema-24`.
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
