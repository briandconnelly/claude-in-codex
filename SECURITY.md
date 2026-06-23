# Security Policy

## Supported Versions

`claude-in-codex` is pre-1.0. Security fixes are released for the latest
published version.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately by contacting the maintainer
through GitHub. Do not open a public issue with exploit details, secrets, or
private workspace contents.

Include:

- The affected version.
- The relevant configuration, including `CLAUDE_IN_CODEX_ACCESS` and
  `CLAUDE_IN_CODEX_CLAUDE_CONFIG` when applicable.
- A minimal reproduction or clear description of the failure mode.
- Whether the issue could expose code, secrets, credentials, or billing risk.

The plugin shells out to the Claude Code CLI and may send gathered context to
Anthropic for paid tools. Secret redaction is best-effort defense in depth, not
a guarantee. Its coverage spans both directions: it redacts the server-gathered
git diff before that diff is sent to Claude, and it scrubs the returned model
output relayed back to the caller (summary, findings, questions, assumptions,
next_steps, the `detail=full` raw response text, and model-derived error
messages). It does **not** cover your free-form inputs (`prompt`, `context`,
`target`, `evidence`, `focus`), which are sent verbatim; nor files Claude reads
directly from the workspace under `access=readonly`, whose contents the `claude`
CLI sends to Anthropic outside this redaction path. Because the output redactor
sees each returned field independently, a key block split across separate fields
is residual risk. This disclosure is mirrored agent-side in each paid tool's
description and in the `data_egress` field of `claude_capabilities`. Use
`access=toolless` when a workspace may contain sensitive data.

The tool allowlist does not govern Claude Code hooks. In `config_mode=inherit`
or `scoped`, workspace `.claude/settings*.json` hooks may run shell before or
during a review. Use `config_mode=safe` or `config_mode=bare` for untrusted
workspaces. Prefer `safe` when you want to preserve normal Claude
authentication; use `bare` when API-key-backed maximum isolation is desired.
