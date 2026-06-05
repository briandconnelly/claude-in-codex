# Security Policy

## Supported Versions

`cc-plugin-codex` is pre-1.0. Security fixes are released for the latest
published version.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately by contacting the maintainer
through GitHub. Do not open a public issue with exploit details, secrets, or
private workspace contents.

Include:

- The affected version.
- The relevant configuration, including `CC_PLUGIN_CODEX_ACCESS` and
  `CC_PLUGIN_CODEX_CLAUDE_CONFIG` when applicable.
- A minimal reproduction or clear description of the failure mode.
- Whether the issue could expose code, secrets, credentials, or billing risk.

The plugin shells out to the Claude Code CLI and may send gathered context to
Anthropic for paid tools. Secret redaction is best-effort defense in depth, not
a guarantee. Use `access=toolless` when a workspace may contain sensitive data.

