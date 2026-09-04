# Changelog

All notable changes to `claude-in-codex` will be documented in this file.

This project uses pre-1.0 semantic versioning. Minor versions may change the
agent-visible MCP surface; patch versions are reserved for compatible fixes.

## Unreleased

Fingerprint `claude-in-codex/0.1/schema-55` (was `schema-50`). The changes are
parameter and metadata descriptions, one added `claude_capabilities` field, and
two **breaking** changes (the `timeout` error's machine semantics and `base`'s
type and acceptance, both below). `meta_fields` is a new required property on
`CapabilitiesResult`, and `CAPABILITIES_SCHEMA` is generated from that model, so
the advertised output-schema shape does move; no value set changed.

### Fixed

- `CAPABILITY_SUMMARY` now separates rules from context, and no longer omits two
  safety rules. It is doubly load-bearing — FastMCP's `instructions=` and the
  `claude-in-codex://capabilities` resource — so it is what a client that never
  installed the skill reads first. Compression had flattened it into one
  undifferentiated paragraph in which hard rules, load-bearing hazards, and pure
  inventory were separated only by periods and identical typography, with nothing
  marking which sentences bind.

  Two rules the shipped skill treats as mandatory were missing entirely: keep
  `access=toolless` when the workspace may hold secrets, and never build
  `system_prompt_append` **or** `focus` from workspace content. The `readonly`
  hazard shipped with its rule missing — it named the danger and stopped.

  The text is now split `RULES:` / `CONTEXT:`. The ceiling moves 1,200 → 1,300:
  the previous one was being met by dropping safety rules, which means it was not
  measuring what it was meant to. Inventory moved to `claude_capabilities` to pay
  for part of the increase. (#180)

- One obligation no longer ships at three different strengths. `claude_status`
  was unconditional in the summary ("check claude_status first"), a two-branch
  conditional in the skill, and discretionary in its own tool description ("use
  first when unsure") — three readings an agent may encounter in any order and
  cannot rank. All three now state the same rule, including the "and again after
  a setup error" half the summary initially still omitted; a test asserts the
  full phrase rather than describing the intent. `idempotency_key`'s description
  no longer opens with "Optional" while the skill calls it mandatory; getting that
  one wrong means paying twice. (#180)

- `CLAUDE_IN_CODEX_MAX_INPUT_BYTES` is now published in
  `claude_capabilities().data_egress`. The summary was its only home, and #180's
  premise for dropping it from there — that all three dropped phrases remained in
  the capabilities payload — held for the other two but not for this one.

- `base` is no longer silently accepted, ignored, and echoed on a scope that
  cannot use it. **Breaking.** `_selector_bounds_error` size-checked `base` on
  every scope, but its *value* was validated only on the branch path, so
  `scope=working_tree` with a bogus `base` returned `ok: true`, ran a
  working-tree review that ignored the ref, and echoed `base` back at the
  payload root. `head` was already rejected this way, so the guard existed for
  one selector and not its sibling.

  The combination is worse than either half: "review my changes against
  origin/main" is naturally written as `scope=working_tree` + `base=origin/main`,
  so the reachable case was a **paid** review of the wrong thing whose own
  response confirmed the caller's wrong belief about what it had covered. A
  non-branch scope now refuses a supplied `base` with `invalid_base`. (#177)

- `base` is now `str | None` defaulting to `None`, rather than `str` defaulting
  to `"main"`. The old default made "omitted" and "explicitly sent `main`"
  indistinguishable, so the narrower fix — rejecting only a *non-default* base —
  would have preserved the exact bug above for `base="main"` while appearing to
  close it. The `main` default now resolves after scope validation. `meta.base`
  is absent on any scope but `branch`, enforced in `bounded_selectors` so it also
  governs the meta rebuilt from an on-disk job record.

  The effective base resolves once per handler, before `meta` is built, so the
  envelope, the prompt, and any stored job record all name the ref the diff was
  actually gathered against. An explicitly empty `base` is refused rather than
  defaulted — an empty string is a value the caller sent, and treating it as
  omission would recreate the silent-wrong-comparison class this change removes.
  `invalid_base`'s published condition now names the wrong-scope case, so an
  agent consulting the catalog does not conclude its ref was wrong and retry on
  the same wrong scope. (#177)


- The `timeout` error no longer advises an unguarded double spend. **Breaking
  for structural recovery.** A sync paid call that exceeded its deadline
  returned `retryable: true` with `action.next_step: "retry_same_call"`, so an
  agent recovering the way this contract tells it to — branching on `action`
  rather than reading prose — re-issued the identical paid call. Whether the
  first call was charged is *unknown* — the deadline also covers startup and
  workspace hooks, and no cost envelope survives — and `idempotency_key` exists
  only on the three `_async` starters, so that retry had no dedup guard at all.
  Possible-and-unrecoverable is all the safety argument needs; asserting a
  definite spend would be the contract claiming something it cannot observe.

  It was also wrong on `retryable`'s own terms: retryable means the identical
  operation may succeed later, which is false for a deterministic scope/timeout
  pair. With `timeout_seconds: 180` and `effort: "xhigh"` as resolved defaults,
  a default-effort review of a real branch reaching this path is not a corner
  case.

  `timeout` is now `retryable: false`. **Branch on `action.next_step`, not on
  `call_tool` alone:** the action is `call_tool` with the twin and full arguments
  when they fit, and `retry_with_changes` naming the twin when the captured
  arguments exceed `REPAIR_ARGS_MAX_BYTES` (8 KB) — which a large prompt makes the
  normal case for exactly the reviews that time out. A client matching only
  `call_tool` silently loses the recovery on the calls most likely to need it. The
  action names the `_async` twin — which survives the deadline — carrying the
  original arguments minus `timeout_seconds`, bounded by
  `REPAIR_ARGS_MAX_BYTES` like every other reconstructed repair. The message
  states that the charge is possible and unrecoverable, and that any next attempt
  is a new paid run. The action carries a stable
  `idempotency_key` derived from the failed call's `request_id`: it does not
  recover the lost spend — nothing can — but it deduplicates retries of the new
  async launch, which is itself a fresh paid run that can lose its reply. Without
  it the server would emit a literally-callable `_async` launch that violates the
  rule the same server publishes: pass `idempotency_key` on every `_async`
  launch. (#178)

- The shipped skill pointed recovery at the wrong field. It described the error
  envelope as `{code, message, repair}` and said "follow `repair`", never
  mentioning `action`, `next_step`, `retryable`, or `details` — so it steered
  agents at free prose while the machine-followable `RepairAction`, whose
  `arguments` are literally callable, went unmentioned. The skill now branches on
  `action.next_step` first and states that a failed paid call is not to be
  re-issued on reflex. (#178)

### Changed

- The advertised `meta` stub no longer enumerates `Meta`'s 25 field names.
  FastMCP inlines the stub into every tool's output schema, so the enumeration
  was billed 14 times on `tools/list` — 6,818 bytes, 8.9% of the whole discovery
  payload, to say one thing fourteen times. The field list moves to
  `claude_capabilities().meta_fields`, which the stub's description now points
  at, and `tools/list` drops from 76,224 to 70,415 bytes (−5,809, −7.6%) at a
  cost of +468 bytes in the capabilities payload. The discovery budget in
  `tests/test_discovery_cost.py` is ratcheted from 76,800 to 72,000 so the
  recovery cannot be silently re-spent. (#179)

  This does not weaken the #143 guarantee it may look like it reverses. That
  enumeration existed to force a new `Meta` field into the contract digest, and
  #143 also began digesting the field names *directly* — `FINGERPRINT_COVERS`
  says so out loud — precisely so the coverage would survive the description not
  carrying it. Adding a `Meta` field still moves the fingerprint, and the
  anti-drift assertion moved with the enumeration to its new home.

- `claude_capabilities().tool_details[].key_optional_params` now lists `detail`
  for the seven tools that accept it and did not advertise it: all six paid
  tools and `claude_job_consume_result`. `claude_job_result` already listed it,
  so two tools taking the same parameter described it differently. This is the
  compact routing metadata an agent reads instead of fetching a full schema, so
  an omission there makes a real parameter undiscoverable at exactly the
  altitude the field exists to serve. `detail` now rides the shared execution
  knob list, and a test checks every tool's entry against its own advertised
  input schema: `detail` parity in both directions, plus detection of any listed
  parameter that does not exist. It is deliberately not a check that every
  accepted parameter is listed — `key_optional_params` is a curated subset, not
  the schema — so another parameter can still be omitted without failing. (#172)

- `focus` on `claude_review_changes` and `claude_review_changes_async` now
  states in its own description the two construction-side rules that
  `system_prompt_append` already stated: never build it from workspace content,
  and never put secrets in it. It also says that `focus` is emphasis rather than
  a filter, that an `_async` job stores the text verbatim in its job record and
  echoes it back in `meta.focus`, and what its byte cap is. Previously the
  description was `"e.g. 'security', 'tests'."` and both rules lived only in the
  shipped skill, so anyone using the MCP server without that skill got neither —
  and the shorter description invited the inference that `focus` was the safer
  of the two fields to populate from a file, which is backwards on the secrets
  axis. (#176)

## 0.9.0 - 2026-09-03

Fingerprint `claude-in-codex/0.1/schema-50` (was `schema-36`). Every client that
pins the fingerprint must re-pin, and the breaking changes below are the reason
to read this section before upgrading rather than after.

Two themes account for most of the release. **Every paid operation is now
recoverable** — all three have background forms, and a launch says in a field
which of the three things it did instead of leaving the caller to infer it.
And **the response is bounded by the server rather than by the caller's tool
arguments** — in 0.8.0 a large enough argument produced a proportionally large
envelope, on success as well as on rejection.

That second guarantee is scoped to tool arguments deliberately. Two echoes are
still proportional to data the server does not choose: `details.allowed_roots`
on `workspace_outside_roots` serializes the client's MCP roots verbatim (200
roots of 4 KB measure an 806 KB envelope), and the repository-derived fields
tracked in #167. Both come from the operator's own configuration rather than
from a tool call, so they are not reachable by an agent composing arguments —
but neither is bounded, and this release does not claim they are.

### Breaking

- **The `claude_ask` and `claude_review_dry_run` aliases are gone.** They were
  deprecated in 0.8.0. Call `claude_consult` and `claude_dry_run`; the arguments
  and results are unchanged. This also closes the one gap in async coverage —
  `claude_ask` was the only paid tool with no `_async` form.

- **A `*_async` launch returns an `outcome` discriminator; branch on it, not on
  `job_id`.** `claude_consult_async`, `claude_review_changes_async` and
  `claude_adversarial_review_async` each return exactly one of:

  | `outcome` | meaning | next step |
  | --- | --- | --- |
  | `started` | a new paid job is running | poll `claude_job_status` |
  | `existing_job` | an `idempotency_key` replay — **may already be finished** | read `status` / `result_available`; poll only while running |
  | `no_changes` | the diff was empty, the spend was skipped, the result is inline | read the payload |

  `job_id`-presence was never able to express this: `started` and `existing_job`
  both carry one, so a caller branching on presence treats a replay of a
  *finished* job as a fresh launch and polls something already terminal. The
  machine-readable table is published at
  `claude_capabilities.async_lifecycle.start_outcome_routing`. The empty-diff
  reply also used to label itself `tool: "claude_review_changes"` when the async
  tool was the one invoked; it now names the tool the caller actually called.

- **Selector arguments are capped, and oversized ones are refused.** `paths` is
  limited to 256 entries, 4096 bytes per entry, and 32,768 bytes in aggregate;
  `base` and `head` to 4096 bytes each. Over any cap the call is rejected
  (`invalid_paths` / `invalid_base` / `invalid_head`) with the limit and the
  actual size in `details`, before any spend.

  The cap is on the input because the *response* was the unbounded thing: `meta`
  echoed `paths`, `base` and `head` verbatim and composed `diff_range` from them.
  A 10,000-character path returned an 11 KB envelope and a 10,000-character
  `base` a 21 KB one — and not only when rejected, because a *valid* oversized
  path was echoed the same way on a successful review. The largest envelope the
  caps now admit is 34.6 KB, at 256 entries against the aggregate limit.

  Nothing legitimate is near these limits; a path longer than 4096 bytes exceeds
  what common filesystems accept.

- **Errors carry the rejected value in `details.value`.** Recovery no longer
  requires parsing `message` prose. The value is escaped inert and bounded to 200
  characters — see *Bounded and inert echoes* below.

- **A sessionless connection must pass `workspace_root`.** On MCP 2026-07-28
  there is no `roots/list` back-channel to ask, and the server does **not** fall
  back to its own working directory — silently reviewing whatever directory the
  server happens to be running in is the wrong answer to "which repo?". Such a
  call fails with `invalid_workspace_root` and
  `details.reason = "roots_unavailable_on_connection"`. Handshake-era
  connections (MCP ≤ 2025-11-25) are unaffected.

### Added

- **`claude_consult_async` and `claude_adversarial_review_async`.** With the
  alias removal above, every paid operation now has both a blocking and a
  background form, so a call that outlives its connection no longer loses work
  that was already paid for. Pass `idempotency_key` and a retry after a dropped
  connection replays the existing job instead of buying a second one.

- **`system_prompt_append`** on `claude_consult`, `claude_review_changes` and
  their async forms: your own persona or focus directive, appended to Claude's
  system prompt. The guardrail prompt always leads and cannot be replaced, and
  the text is bracketed by markers whose closing side restates that the
  guardrails outrank anything between them. Capped at 4096 bytes and rejected
  before spend. `meta.system_prompt_append` records a SHA-256 and byte length —
  never the text — so a result shows it ran under a non-default prompt.

- **`meta.focus`**, recording the `focus` a review ran under, so a narrowed
  verdict is not mistaken for a broad one. `focus` is now framed and bounded as
  untrusted caller text, like `system_prompt_append`.

- **`paths_matched`**, reported by `claude_dry_run` as well as by paid reviews.
  A path-filter entry that matches nothing used to be invisible; a zero can now
  be read from the free preview instead of after paying for an empty review.

### Changed

- **Bounded and inert echoes, everywhere caller or foreign text reaches an
  envelope.** In 0.8.0 several fields were an unbounded function of their input.
  Now: every echoed value is size-bounded, and control characters and terminal
  escapes are rendered inert rather than shipped live into output an agent
  displays.

  - `message` echoes render through `repr()`, bounded to 200 characters.
  - `details.value` is escaped bare — ordinary values come back byte-for-byte,
    while escapes and lone surrogates are defanged — and bounded to the same 200.
    The bound is applied *after* escaping, because the envelope's
    `backslashreplace` pass expands a lone surrogate to six characters: a cap
    counted before it let 200 "characters" ship as ~1,200.
  - Foreign subprocess output (git's stderr, reached through `internal_error`)
    is sanitized and bounded to 400 UTF-8 bytes, keeping the first line *and* a
    tail, since git leads with the diagnosis and follows with hints.

  A truncation marker always shows, so a cut value never reads as a complete one,
  and the marker counts against the budget rather than being added on top of it.

- **FastMCP 4, on MCP Python SDK v2 (`mcp` 2.1.1).** This is what lets the server
  serve both handshake eras, including the sessionless 2026-07-28 connections the
  workspace rule above concerns.

- **Discovery cost: the `tools/list` budget is 76,800 bytes** (was 66,000). Two
  new async tools, `system_prompt_append`, `focus` and the `outcome` routing
  table all have to be discoverable; the budget is a tested ceiling, not a
  target.

- **Named job tools rather than native MCP tasks**, with the reasoning recorded
  in `COMPATIBILITY.md`: a paid call's handle must outlive the process that
  issued it, and FastMCP's task store is in-process, so a restart would lose a
  run that had already spent money.

### Fixed

- **Git calls no longer inherit `GIT_*` environment variables.** `GIT_DIR` and
  friends override repository discovery, so an inherited value — a git hook
  exports one — pointed the server's git commands at a different repository than
  the caller's workspace.

- **Unencodable user text is refused before spend**, on the async launch path as
  well as the blocking one, instead of crashing partway through a paid run.

- **`paths` values no longer reach Claude inside a sentence written in the
  server's own voice**, where a crafted path could read as an instruction from
  the server rather than as caller data.

- **The `invalid_arguments` envelope survives FastMCP 3.4.3+**, which would
  otherwise have converted an argument-shape failure into a prose-only
  `isError: true` with no code and no repair — an undisclosed third error carrier.

- **`invalid_workspace_root` now advertises the `reason` field it emits.** The
  catalog published only `field`/`value`, so a client building a branch from
  `claude_capabilities` could not discover the token that distinguishes "the path
  you passed is not a directory" from "this connection cannot be asked for
  roots" — the two cases have different repairs.

### Internal

- The fingerprint gate covers `Meta` field names directly, not only via the
  advertised description that enumerates them.
- Every emitted `details` field is asserted to be advertised in the error
  catalog, checked at the error-builder boundary so it holds for every branch
  the suite reaches rather than one specimen per code.
- The live integration tests can now fail on structured-output drift. The
  assertion previously accepted `unknown`, which is exactly what the normalizer
  emits when Claude returns no structured JSON at all — so it passed whether
  structured output worked or was never exercised.

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
