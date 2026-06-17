# cc-plugin-codex

[![CI](https://github.com/briandconnelly/cc-plugin-codex/actions/workflows/ci.yml/badge.svg)](https://github.com/briandconnelly/cc-plugin-codex/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](./pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

Ask Claude Code for an independent code review or second opinion, straight from Codex.

`cc-plugin-codex` is review-only: Claude reviews, critiques, and advises. It does not edit
your code, run shell commands, or get write tools. It is the mirror image of
[`openai/codex-plugin-cc`](https://github.com/openai/codex-plugin-cc), which lets Claude call
Codex.

## What it does

- Lets Codex call the Claude Code CLI through MCP.
- Sends Claude bounded context for reviews, critiques, and second opinions.
- Returns structured findings Codex can summarize or act on.
- Keeps Codex as the actor and Claude as a read-only reviewer.

## Quickstart

You need:

- Codex
- the `claude` CLI, installed and authenticated
- Python 3.11 or newer
- `uvx`
- `git`

Check the basics:

```sh
claude --version
claude /login
uvx --version
git --version
```

Add this repository as a Codex marketplace, then install the plugin from it:

```sh
codex plugin marketplace add briandconnelly/cc-plugin-codex
codex plugin add cc-plugin-codex
```

Restart Codex after installing. Then ask Codex:

> Ask Claude to run `claude_status`.

`claude_status` is free. It checks whether the `claude` CLI is installed, authenticated, and
compatible, and shows the defaults a paid call would use.

## Use it

Once `claude_status` reports `ready: true`, ask Codex in plain language:

> Ask Claude to review my current diff. Pass `workspace_root` as this repository.

Passing `workspace_root` matters. It keeps reviews pointed at your project instead of the
plugin install directory when the MCP client does not provide a repo root.

Other useful prompts:

- "Ask Claude to review my staged changes for security issues."
- "Have Claude attack this plan for weaknesses."
- "Get an independent second opinion from Claude on this design."
- "Start a background Claude review of this branch against main."

Use this when you want a second model to look for bugs, regressions, missing tests, security
issues, or weak assumptions before you merge or commit to a design.

Codex uses the plugin skill to choose the right tool and arguments. Direct MCP calls are also
available:

| Tool | Use | Cost |
| --- | --- | --- |
| `claude_review_changes` | Review a git diff now | paid |
| `claude_review_changes_async` | Start a background diff review | paid |
| `claude_adversarial_review` | Pressure-test a plan, claim, or change | paid |
| `claude_ask` | Ask for a free-form second opinion | paid |
| `claude_status` | Check readiness and defaults | free |
| `claude_review_dry_run` | Preview diff/context before a review | free |

Diff review scopes are `working_tree`, `staged`, and `branch`.

Review staged changes with a test-coverage focus:

```json
claude_review_changes({
  "workspace_root": "/absolute/path/to/your/repo",
  "scope": "staged",
  "focus": "tests"
})
```

For a long review, launch it in the background:

```json
claude_review_changes_async({
  "workspace_root": "/absolute/path/to/your/repo",
  "scope": "branch",
  "base": "main"
})
```

Poll with `claude_job_status`, then fetch the result with `claude_job_result`.

## Safety and cost

- Paid tools run Claude and send code or prompts to Anthropic, billed through your existing
  `claude` login or `ANTHROPIC_API_KEY`.
- Free tools only inspect local state, preflight a request, or manage background jobs.
- Claude never receives write or Bash tools from this plugin. Claude Code hooks are not
  tools and may run in `config_mode=inherit`/`scoped`; use `config_mode=safe` or
  `config_mode=bare` for untrusted workspaces.
- `access=toolless` is the default: Claude receives gathered context as text and cannot read
  more files. `access=readonly` lets Claude use `Read`, `Grep`, and `Glob` for extra context.
- Secret redaction is best-effort defense in depth. Use `access=toolless` when a workspace may
  contain secrets.
- `max_budget_usd` is a best-effort Claude CLI stop threshold, not a hard cap. Results report
  actual spend in `meta.cost_usd` when available.
- Reviews default to `effort=xhigh` for depth. Lower `effort` to `high` or `medium` for routine
  reviews when cost matters.

If a requested diff scope has no changes, the review tools return a passing result without
invoking Claude.

## Mental model

Codex remains responsible for deciding what to do with Claude's feedback. Claude receives only
the context the plugin provides, or read-only file access when you explicitly allow
`access=readonly`. The plugin does not give Claude write tools, Bash tools, or permission to
modify your workspace. In `inherit` and `scoped`, Claude Code may still load workspace hooks
from `.claude/settings*.json`; those hooks execute outside the tool allowlist.

## Common knobs

Every setting is optional. These are the knobs most users are likely to change:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CC_PLUGIN_CODEX_ACCESS` | `toolless` | `toolless` or `readonly` |
| `CC_PLUGIN_CODEX_CLAUDE_CONFIG` | `inherit` | `inherit`, `scoped`, `safe`, or `bare` |
| `CC_PLUGIN_CODEX_EFFORT` | `xhigh` | `low`, `medium`, `high`, `xhigh`, or `max` |
| `CC_PLUGIN_CODEX_MAX_BUDGET_USD` | `1.00` | best-effort per-call budget threshold |
| `CC_PLUGIN_CODEX_MODEL` | unset | Claude model; unset uses the CLI default |
| `CC_PLUGIN_CODEX_TIMEOUT_SECONDS` | `180` | per-call timeout, clamped to 10-600 seconds |
| `ANTHROPIC_API_KEY` | unset | required only for `config_mode=bare` |

Set these in the environment you launch Codex from. The bundled MCP config forwards the common
cost, safety, model, timeout, and API-key variables to the server.

`config_mode=inherit` uses your normal Claude environment without persisting a session.
`scoped` drops user-global settings and user MCP servers but keeps `CLAUDE.md` and workspace
hooks. `safe` disables Claude Code customizations and hooks while preserving normal
authentication. `bare` strips `CLAUDE.md`, memory, and hooks, and requires
`ANTHROPIC_API_KEY`.

## Troubleshooting

Start with:

> Ask Claude to run `claude_status`.

Then:

- If `claude_authenticated` is false, run `claude /login`.
- If the workspace looks wrong, pass `workspace_root` explicitly.
- If a review is large or expensive, run `claude_review_dry_run` first.
- If a background job id is lost, use `claude_job_list`.
- If `config_mode=bare` fails, confirm `ANTHROPIC_API_KEY` is set in the environment that
  launches Codex.

## Distribution

The Codex plugin install path is the primary user-facing path. The bundled MCP config pins the
server to a versioned Git tag so installed users update deliberately.

The Python package publishes the MCP server entry point for direct use and release provenance.
After a PyPI release, the server can also be launched with:

```sh
uvx --from cc-plugin-codex==0.5.0 cc-plugin-codex-mcp
```

## Advanced reference

Every tool returns a structured `ok`/`error` envelope. Paid results include usage metadata and
cost when the Claude CLI reports it. The tool contract is experimental and pre-1.0; clients can
pin `meta.fingerprint` to detect agent-visible changes.

The Claude CLI compatibility assumptions are centralized in
[`src/cc_plugin_codex/cli_contract.py`](./src/cc_plugin_codex/cli_contract.py) and documented
in [`COMPATIBILITY.md`](./COMPATIBILITY.md).

## Local development

Run the MCP server from a checkout:

```sh
codex mcp add cc-plugin-codex -- uv run --directory "$(pwd)" cc-plugin-codex-mcp
```

Run tests:

```sh
uv run pytest
```

The full suite enforces a 95% coverage floor. For one-file iteration:

```sh
uv run pytest tests/test_jobs.py --no-cov
```

Live Claude integration tests are excluded by default:

```sh
uv run pytest -m integration --no-cov
```

They require the `claude` CLI and make a small paid API call.
