# cc-plugin-codex

Call Claude Code from Codex for bounded, independent code review and second opinions.
The mirror image of [`openai/codex-plugin-cc`](https://github.com/openai/codex-plugin-cc)'s
review surface.
Unlike that plugin, cc-plugin-codex is review-only: it does not delegate write-capable
tasks. Reviews can run synchronously or as background jobs, but Claude only ever reviews.

## What it does

An MCP server wraps the `claude` CLI and exposes read-only tools to Codex:
`claude_ask`, `claude_review_changes`, `claude_adversarial_review`, and `claude_status`,
plus `claude_review_changes_async` with `claude_job_status`/`claude_job_result`/
`claude_job_consume_result`/`claude_job_cancel`/`claude_job_list` for background reviews.
`claude_review_dry_run` previews workspace/diff-size/redaction for free before a paid
review, and `cc_codex_capabilities` (alias `claude_capabilities`) returns the capability
contract. Claude reviews; it never edits your code.

Each tool publishes an output schema describing the `ok`-discriminated result
(`{"ok": true, ...}` on success, `{"ok": false, "error": {code, message, repair}, ...}`
on failure). Failures also set the MCP `isError` flag, so branch on either `ok` or
`isError`. The surface is **experimental / pre-1.0**; clients should pin
`meta.fingerprint` to detect schema changes.
Invalid values for enum-typed parameters (`config_mode`, `access`, `scope`, `detail`)
are rejected by the MCP framework as a schema validation error before the tool runs,
rather than via the `ok:false` envelope; every other failure uses the envelope.
The capability fingerprint changes whenever the agent-visible contract changes.
Deprecated tools remain discoverable during their compatibility window, with their
replacement named in the tool description and capability summary.

The paid tools (`claude_ask`, `claude_review_changes`, `claude_adversarial_review`)
**block synchronously** for up to `timeout_seconds` (default 180s, max 600s).
They can be **cancelled** by the client, which terminates the underlying Claude process, but not resumed.
Call `claude_status` first — it is
free and reports the resolved defaults (`config_mode`, `access`, `model`, `effort`,
clamped `max_budget_usd`/`timeout_seconds`, and the clamp bounds) a no-argument paid call
would use, so you can predict cost and behavior before spending.
It also runs free readiness probes — `claude_authenticated` (so you can catch a
logged-out CLI before paying for a call that would only then fail auth),
`version_supported` (whether the installed CLI matches the supported major), and a
combined `ready` flag.
Every paid result also reports `meta.cost_usd` and `meta.usage` (token counts) so an
agent can track actual spend across calls.
Diff-bearing paid results report `meta.redacted_paths` when the server withheld or
masked content before sending it to Claude.

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

## Local dev

This v1 runs from the local checkout (not yet published to PyPI).
`codex mcp add cc-plugin-codex -- uv run --directory "$(pwd)" cc-plugin-codex-mcp`

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

## Workspace

The diff-bearing tools operate on a workspace resolved in this order: an explicit
`workspace_root` argument, then the client's first MCP root, then the server's own
working directory.
`meta.workspace_source` reports which rule applied.
When the client provides MCP roots, an explicit `workspace_root` must be contained
inside one of those roots; otherwise the tool returns `workspace_outside_roots`.
Clients that do not provide roots may still pass any existing absolute directory.
Pass `workspace_root` explicitly when launching the server from a fixed directory (e.g. a
plugin install) so reviews target your project rather than the plugin checkout.

## Safety

- Read-only: Claude is never given write or Bash tools.
- Secret redaction combines filename-based rules (`.env`, `*.env`, `*.pem`, `*.key`,
  key files) with conservative content scanning for high-confidence token/key patterns
  in gathered diff lines. Treat it as defense-in-depth, not a guarantee.
- Diff redaction only applies to the context the server gathers. With `access=readonly`,
  Claude can read any file in the workspace directly (`Read`/`Grep`/`Glob`), so redaction
  does NOT protect against secrets it reads itself — use `access=toolless` (the default)
  when the workspace may contain secrets.
- All `config_mode`s drop your other MCP servers, but `inherit`/`scoped` still load your
  user-level Claude hooks and settings; use `config_mode=bare` for full isolation.
- Each call is paid and sends code to Anthropic. `max_budget_usd` is a best-effort stop
  threshold (enforced by the Claude CLI), not a hard cap — reported `meta.cost_usd` can
  exceed it; `meta.requested_max_budget_usd` echoes the value sent. `timeout_seconds`
  bounds wall-clock time per call.
- Free-form text inputs (`prompt`/`context` for `claude_ask`, `target`/`evidence` for
  `claude_adversarial_review`) are capped before a paid call by
  `CC_PLUGIN_CODEX_MAX_INPUT_BYTES` (default 200000 bytes).
- Git context collection is bounded by `CC_PLUGIN_CODEX_GIT_TIMEOUT_SECONDS` (default
  60s), so preflight diff work cannot hang indefinitely.

If a requested diff scope has no changes, the review tools return `ok:true` with a
`pass` verdict and a "No changes in scope" summary without invoking Claude or starting
a background job.

## Background reviews

`claude_review_changes_async` launches a diff review as a detached job and returns a
handle `{ok, job_id, status:"running", …}` immediately instead of blocking the Codex
turn. Poll `claude_job_status(job_id)`, then call `claude_job_result(job_id)` once
`result_available` is true — the result is the **same** `ok`/`verdict`/`findings`
envelope the synchronous tool returns, with `meta.job_id` set. `claude_job_result`
is read-only and leaves the stored record available until TTL cleanup. Use
`claude_job_consume_result(job_id)` to fetch a finished result and delete the record.
`claude_job_cancel` terminates a running job. The status/result/consume/cancel tools
are free.

The diff is gathered at launch (same secret redaction and `--max-budget-usd` stop
threshold as the sync path). Because the server drives one-shot `claude -p --output-format json`, a job's
completion is simply "the process exited and wrote its JSON envelope" — no interactive
log scraping. State lives on disk keyed by workspace, so status/result/cancel survive an
MCP server restart. There is no daemon: overrunning jobs are stopped on the next status
poll (deadline `CC_PLUGIN_CODEX_JOB_MAX_SECONDS`, default 1800s). Job start/status
responses include `poll_after_ms`, `ttl_seconds`, and `expires_at` where known.
Records are cleaned up after `CC_PLUGIN_CODEX_JOB_TTL` (default 24h), and the budget
stop threshold still applies (best-effort) even for a job nobody polls. Job records (which contain the diff)
are stored under
`CC_PLUGIN_CODEX_STATE_DIR` (default `~/.cache/cc-plugin-codex/jobs`); anyone with access
to that workspace's state directory can read or cancel its jobs.

## Reasoning effort (`effort`)

Each paid tool accepts `effort` (`low|medium|high|xhigh|max`), passed through to the
`claude` CLI's `--effort`. It defaults to `xhigh` — review depth is the whole point of
this server. Lower it (`high`/`medium`) to trade rigor for cost on routine reviews, or
set the default with `CC_PLUGIN_CODEX_EFFORT`. An invalid per-call `effort` is a typed
enum, so it is rejected as a schema validation error before the tool runs; an
unrecognized `CC_PLUGIN_CODEX_EFFORT` env value instead falls back to the default.

## Environment variables

`CC_PLUGIN_CODEX_CLAUDE_CONFIG`, `CC_PLUGIN_CODEX_ACCESS`, `CC_PLUGIN_CODEX_MODEL`,
`CC_PLUGIN_CODEX_EFFORT`, `CC_PLUGIN_CODEX_MAX_BUDGET_USD`,
`CC_PLUGIN_CODEX_TIMEOUT_SECONDS`, `CC_PLUGIN_CODEX_MAX_INPUT_BYTES`,
`CC_PLUGIN_CODEX_GIT_TIMEOUT_SECONDS`, `CC_PLUGIN_CODEX_SUPPORTED_MAJORS`,
`ANTHROPIC_API_KEY`.

Background jobs add: `CC_PLUGIN_CODEX_STATE_DIR`, `CC_PLUGIN_CODEX_JOB_MAX_SECONDS`,
`CC_PLUGIN_CODEX_JOB_TTL`, `CC_PLUGIN_CODEX_JOB_MAX_COUNT`.

## Compatibility

This server couples to the external `claude` CLI (and the Codex host).
Every assumption — flags, JSON-envelope keys, subcommands, accepted effort levels,
supported version range — is centralized in `src/cc_plugin_codex/cli_contract.py`
and documented in [COMPATIBILITY.md](./COMPATIBILITY.md), which also covers what
breaks on an upstream change and how to fix it.
When a guarantee-bearing flag or the CLI contract drifts, paid tools fail loudly
with a `cli_contract_changed` error (no silent weakening, no spend) and
`claude_status` surfaces a version or flag warning for free.
