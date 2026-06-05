# cc-plugin-codex

Ask Claude Code for an independent code review or second opinion, straight from Codex.

cc-plugin-codex is **review-only**: Claude reviews, critiques, and advises — it never edits
your code, runs shell, or delegates write tasks. Reviews can run synchronously or as
background jobs. It's the mirror image of
[`openai/codex-plugin-cc`](https://github.com/openai/codex-plugin-cc), which goes the other
direction.

## What it does

An MCP server wraps the `claude` CLI and exposes read-only tools to Codex. The plugin itself
is free and open-source — there's nothing to buy here. In the table below, **"paid"** marks
tools that run Claude (an inference call billed to your existing `claude` login or
`ANTHROPIC_API_KEY`, the same as running `claude` yourself) and report their cost via
`meta.cost_usd`; **"free"** tools only read local state or preflight, so they never invoke
Claude and never spend. Every result is a structured `ok`/`error` envelope — see
[Result format & compatibility](#result-format--compatibility) — which is what makes the
surface agent-friendly.

| Tool | Purpose | Cost |
| --- | --- | --- |
| `claude_ask` | Free-form question / second opinion | paid |
| `claude_review_changes` | Review the current diff, synchronously | paid |
| `claude_adversarial_review` | Attack a plan, claim, or change for weaknesses | paid |
| `claude_review_changes_async` | Launch a diff review as a background job | paid |
| `claude_job_status` · `claude_job_result` · `claude_job_consume_result` · `claude_job_cancel` · `claude_job_list` | Poll, fetch, consume, cancel, or list background jobs | free |
| `claude_status` | Readiness probes + the resolved defaults a paid call would use | free |
| `claude_review_dry_run` | Preview workspace / diff size / redaction before a paid review | free |
| `cc_codex_capabilities` (alias `claude_capabilities`) | Return the capability contract, including compact per-tool routing metadata | free |

Paid tools **block synchronously** for up to `timeout_seconds` (default 180s, max 600s) and
can be cancelled (terminating the underlying Claude process) but not resumed. Each paid
result reports `meta.cost_usd` and `meta.usage` (token counts) so you can track spend.

Call the free **`claude_status`** first: it reports the resolved defaults a no-argument paid
call would use (`config_mode`, `access`, `model`, `effort`, clamped
`max_budget_usd`/`timeout_seconds`, and a practical minimum-budget hint) plus readiness
probes — `claude_authenticated` (catch a logged-out CLI before paying),
`version_supported`, and a combined `ready` flag.

## Requirements

- The `claude` CLI installed and authenticated (`claude /login`), and `git`.
- `uv` for running the server.
- `config_mode=bare` additionally requires `ANTHROPIC_API_KEY`.

## Install

Add this repository as a Codex marketplace, then install `cc-plugin-codex` from it:

```
codex plugin marketplace add briandconnelly/cc-plugin-codex
```

The bundled MCP config runs the server with `uvx` directly from this repository.

## Usage

Just ask Codex in plain language — it picks the right tool:

- "Ask Claude to review my current diff."
- "Have Claude attack this plan for weaknesses."
- "Get an independent second opinion from Claude."

Those map to `claude_review_changes`, `claude_adversarial_review`, and `claude_ask`. The diff
tools take a `scope` (`working_tree`, `staged`, or `branch`) and an optional `focus`:

```json
claude_review_changes({ "scope": "staged", "focus": "security" })
```

For a long review, launch it in the background instead of blocking the turn:

```json
claude_review_changes_async({ "scope": "branch", "base": "main" })
```

It returns a job handle immediately; poll `claude_job_status(job_id)` and fetch with
`claude_job_result(job_id)`. Run the free `claude_review_dry_run` first to see exactly what
diff would be sent (and what gets redacted) without spending.

## Config modes (`config_mode`)

| Mode | Isolation | Auth |
| --- | --- | --- |
| `inherit` (default) | normal Claude env, no persisted session | your existing login |
| `scoped` | drops user-global settings + user MCP servers; keeps CLAUDE.md | your existing login |
| `bare` | strips CLAUDE.md/memory/hooks | requires `ANTHROPIC_API_KEY` |

Known limitation: in `claude 2.1.x` there is no OAuth-preserving way to fully strip
`CLAUDE.md`/memory — full independence (`bare`) requires an API key.

## Access modes (`access`)

`toolless` (default) sends Claude the diff as text; `readonly` lets Claude use `Read,Grep,Glob`
to pull extra context. Claude never gets write or Bash tools.

## Reasoning effort (`effort`)

Each paid tool accepts `effort` (`low|medium|high|xhigh|max`), passed to the `claude` CLI's
`--effort`. It defaults to `xhigh` — review depth is the whole point of this server. Lower it
(`high`/`medium`) to trade rigor for cost on routine reviews, or set the default with
`CC_PLUGIN_CODEX_EFFORT`. An invalid per-call value is rejected before the tool runs; an
unrecognized env value falls back to the default.

## Workspace

The diff tools resolve their workspace in this order: an explicit `workspace_root` argument,
then the client's first MCP root, then the server's own working directory
(`meta.workspace_source` reports which applied). When the client provides MCP roots, an
explicit `workspace_root` must be inside one of them, or the tool returns
`workspace_outside_roots`. **Pass `workspace_root` explicitly when the server launches from a
fixed directory (e.g. a plugin install)** so reviews target your project rather than the
plugin checkout.

## Safety & cost

- **Read-only:** Claude is never given write or Bash tools.
- **Paid:** each call sends code to Anthropic. `max_budget_usd` is a best-effort stop
  threshold (enforced by the Claude CLI), not a hard cap — reported `meta.cost_usd` can exceed
  it; `meta.requested_max_budget_usd` echoes the value sent. Very low budgets are mostly
  useful as failure tests: even small asks often need roughly `$0.10-$0.20`, and real
  reviews cost more. Lower best-effort budgets can still spend and return
  `budget_exceeded` without a useful answer. `timeout_seconds` bounds wall-clock time per call.
- **Secret redaction** combines filename rules (`.env`, `*.pem`, `*.key`, key files) with
  conservative content scanning for token/key patterns in gathered diff lines — treat it as
  defense-in-depth, not a guarantee. It only covers context the *server* gathers: with
  `access=readonly` Claude can read files directly, so use `access=toolless` (the default)
  when the workspace may contain secrets.
- **Isolation:** all `config_mode`s drop your other MCP servers, but `inherit`/`scoped` still
  load your user-level Claude hooks and settings; use `bare` for full isolation.
- Free-form inputs (`prompt`/`context`, `target`/`evidence`) are capped before a paid call by
  `CC_PLUGIN_CODEX_MAX_INPUT_BYTES`; git context collection is bounded by
  `CC_PLUGIN_CODEX_GIT_TIMEOUT_SECONDS`.

If a requested diff scope has no changes, the review tools return `ok:true` with a `pass`
verdict and a "No changes in scope" summary without invoking Claude or starting a job.

## Background reviews

`claude_review_changes_async` launches a diff review as a detached job and returns a handle
immediately. The diff is gathered **at launch** (same redaction and budget stop threshold as
the sync path). Poll `claude_job_status(job_id)`, then call `claude_job_result(job_id)` once
ready — it returns the **same** envelope the sync tool does and leaves the record in place;
`claude_job_consume_result` fetches and deletes it; `claude_job_cancel` stops a running job.
All status/result/cancel tools are free.

State lives **on disk keyed by workspace** (under `CC_PLUGIN_CODEX_STATE_DIR`), so jobs survive
an MCP server restart. There is no daemon: single-job lifecycle calls refresh and TTL-clean the
requested job, `claude_job_list` cleans the workspace, and the count cap is enforced when jobs
start. Overrunning jobs are reaped when that job is polled or listed
(`CC_PLUGIN_CODEX_JOB_MAX_SECONDS`), and terminal records are cleaned up after
`CC_PLUGIN_CODEX_JOB_TTL` on the same lazy-maintenance schedule. Job records contain the
gathered diff, so anyone with access to that workspace's state directory can read or cancel its
jobs.

## Environment variables

Every value below has a code-side default, so the server runs with none of these set.
All are read at process start; they tune defaults, cost/safety bounds, and background-job
storage.

| Variable | Description | Default / example |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API key. Required only for `config_mode=bare`; ignored by `inherit`/`scoped`, which use your existing `claude` login. | _(unset)_ — e.g. `sk-ant-…` |
| `CC_PLUGIN_CODEX_ACCESS` | Default access mode: `toolless` (diff sent as text) or `readonly` (Claude may use `Read,Grep,Glob`). | `toolless` |
| `CC_PLUGIN_CODEX_CLAUDE_CONFIG` | Default config mode / isolation: `inherit`, `scoped`, or `bare`. | `inherit` |
| `CC_PLUGIN_CODEX_EFFORT` | Default reasoning effort: `low`, `medium`, `high`, `xhigh`, or `max`. An unrecognized value falls back to the default. | `xhigh` |
| `CC_PLUGIN_CODEX_GIT_TIMEOUT_SECONDS` | Wall-clock cap for git context collection (preflight diff work). Floored at 1s. | `60` |
| `CC_PLUGIN_CODEX_JOB_MAX_COUNT` | Retained background-job records per workspace; oldest terminal jobs are evicted past this. | `50` |
| `CC_PLUGIN_CODEX_JOB_MAX_SECONDS` | Wall-clock deadline for a background job; a status poll past this reaps it. | `1800` |
| `CC_PLUGIN_CODEX_JOB_TTL` | Seconds before terminal background-job records are cleaned up. | `86400` |
| `CC_PLUGIN_CODEX_MAX_BUDGET_USD` | Default per-call budget stop threshold (best-effort, not a hard cap). Clamped to 0.01–5.00. | `1.00` |
| `CC_PLUGIN_CODEX_MAX_INPUT_BYTES` | Cap on free-form text inputs (`prompt`/`context`/`target`/`evidence`) before a paid call. Floored at 1000. | `200000` |
| `CC_PLUGIN_CODEX_MODEL` | Default Claude model. Unset inherits the `claude` CLI default. | _(unset)_ — e.g. `claude-opus-4-8` |
| `CC_PLUGIN_CODEX_STATE_DIR` | Directory for background-job state (job records contain the gathered diff). | `~/.cache/cc-plugin-codex/jobs` |
| `CC_PLUGIN_CODEX_SUPPORTED_MAJORS` | Comma-separated `claude` CLI major versions to accept; override to opt into an untested major. | `2` |
| `CC_PLUGIN_CODEX_TIMEOUT_SECONDS` | Default per-call wall-clock timeout. Clamped to 10–600. | `180` |

### Setting them under Codex

Codex does not expand `${VAR}` syntax in MCP config ([codex#2680](https://github.com/openai/codex/issues/2680)),
so the bundled `.mcp.json` forwards a curated subset of these from your environment via
`env_vars` — the common cost/safety/model knobs plus `ANTHROPIC_API_KEY`:

```json
"env_vars": [
  "ANTHROPIC_API_KEY",
  "CC_PLUGIN_CODEX_ACCESS",
  "CC_PLUGIN_CODEX_CLAUDE_CONFIG",
  "CC_PLUGIN_CODEX_EFFORT",
  "CC_PLUGIN_CODEX_MAX_BUDGET_USD",
  "CC_PLUGIN_CODEX_MODEL",
  "CC_PLUGIN_CODEX_TIMEOUT_SECONDS"
]
```

Set any of these in the environment you launch Codex from and the value is passed through;
leave it unset and the code default above applies. To forward one of the variables not in the
curated list (e.g. `CC_PLUGIN_CODEX_STATE_DIR`), add its name to `env_vars`, or define the
server explicitly in `~/.codex/config.toml`.

## Result format & compatibility

Each tool publishes an output schema describing the `ok`-discriminated result
(`{"ok": true, ...}` on success, `{"ok": false, "error": {code, message, repair}, ...}` on
failure). Failures also set the MCP `isError` flag, so branch on either `ok` or `isError`.
Invalid values for enum-typed parameters (`config_mode`, `access`, `scope`, `detail`) are
rejected by the MCP framework as a schema validation error before the tool runs; every other
failure uses the envelope. Diff-bearing paid results report `meta.redacted_paths` when the
server withheld or masked content before sending it to Claude.

The surface is **experimental / pre-1.0**; clients should pin `meta.fingerprint`, which
changes whenever the agent-visible contract changes. Deprecated tools stay discoverable during
their compatibility window, with their replacement named in the tool description and
capability summary.

This server couples to the external `claude` CLI (and the Codex host). Every assumption —
flags, JSON-envelope keys, subcommands, accepted effort levels, supported version range — is
centralized in `src/cc_plugin_codex/cli_contract.py` and documented in
[COMPATIBILITY.md](./COMPATIBILITY.md), which covers what breaks on an upstream change and how
to fix it. When a guarantee-bearing flag or the CLI contract drifts, paid tools fail loudly
with a `cli_contract_changed` error (no silent weakening, no spend) and `claude_status`
surfaces a version or flag warning for free.

## Local dev

This v1 runs from the local checkout (not yet published to PyPI):

```
codex mcp add cc-plugin-codex -- uv run --directory "$(pwd)" cc-plugin-codex-mcp
```

### Tests

```
uv run pytest
```

The full suite enforces a 95% coverage floor. When iterating on a single file,
bypass the gate with `--no-cov`:

```
uv run pytest tests/test_jobs.py --no-cov
```

The live Claude integration tests are excluded by default; run them with
`uv run pytest -m integration --no-cov` (requires the `claude` CLI, and makes a
small paid API call).
