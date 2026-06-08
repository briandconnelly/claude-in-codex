# Agent Instructions

This repository is configured for agent-assisted work through GitHub pull requests.
Safety-critical controls are enforced in GitHub rulesets, CODEOWNERS, required
checks, and workflow permissions; do not weaken or bypass those controls unless a
human explicitly asks for that change.

## Workflow

- Work on a branch and open a pull request to `main`.
- Do not push directly to protected refs.
- Link the relevant issue or context in the PR description when available.
- Preserve commit attribution. Sign commits when your local setup supports it.
  Add human co-authors when pairing.
- Keep history linear; use squash or rebase merge methods.
- After any post-review push, request fresh human review.

## Review And Ownership

- Changes to owned paths require human CODEOWNERS review before merge.
- Treat `.github/`, `CODEOWNERS`, release/publish workflows, dependency files,
  and security-sensitive configuration as high-risk.
- Do not self-approve, dismiss reviews to clear your own change, alter review
  requests to satisfy approval requirements, or enable auto-merge in a way that
  avoids human review.

## CI And Validation

- Required checks must pass before merge.
- Run focused tests for the area you changed. Common commands:

```sh
uv run prek run --all-files
uv run pytest
uv run pytest tests/test_jobs.py --no-cov
```

- The full test suite enforces the repository coverage floor. Use narrower
  commands during iteration, then run the relevant broader checks before PR.
- CI runs Ruff, ty, and pytest across the supported Python versions. Before PRs
  that affect shipped code, prefer the same local gates: `uv run ruff check src
  tests`, `uv run ruff format --check src tests`, `uv run ty check`, and
  `uv run pytest -q`.
- Live Claude integration tests are opt-in, paid, and may send context to
  Anthropic. Run `uv run pytest -m integration --no-cov` only when changing
  Claude invocation, authentication, config-mode behavior, or before a release,
  unless a human asks for it.

## Workflows And Supply Chain

- Keep `GITHUB_TOKEN` permissions least-privilege.
- Pin third-party GitHub Actions to full commit SHAs.
- Do not add `pull_request_target` or `workflow_run` workflows unless the
  privileged/untrusted-input risks are explicitly reviewed.
- Bind untrusted GitHub event data through `env:` before using it in shell steps.
- Use `uv.lock` for dependency changes. Prefer `uv add` or `uv remove` for
  dependency edits, commit `pyproject.toml` and `uv.lock` together, and verify
  with `uv sync --locked`. Dependabot PRs go through the same review and CI
  requirements as other changes.
- Do not commit secrets, local credentials, generated caches, or local tool state.

## Project Conventions

- For release/version changes, keep the lockstep files coordinated:
  `pyproject.toml`, `.codex-plugin/plugin.json`, `.mcp.json`, `README.md`, and
  `CHANGELOG.md`. CI enforces this; follow `COMPATIBILITY.md` for the release
  procedure.
- Do not change the `.mcp.json` `@vX.Y.Z` ref except as part of an intentional,
  coordinated release/version bump.
- Changelog entries live in `CHANGELOG.md` under `## X.Y.Z - YYYY-MM-DD`. This
  project uses pre-1.0 semantic versioning: minor versions may change the
  agent-visible MCP surface, and patch versions are for compatible fixes.
- Agent-visible MCP surface changes must bump `FINGERPRINT` in
  `src/cc_plugin_codex/schemas.py` and update the golden-envelope tests in the
  same PR. This includes tool names, input/output schemas, value enums, error
  codes, and capability text.
- Claude CLI compatibility assumptions belong in
  `src/cc_plugin_codex/cli_contract.py`. Keep guarantee-bearing flags fail-closed
  rather than silently weakening cost, access, isolation, or behavior guarantees.
- `.agents/skills` is the canonical repo-owned skill location. Edit skill files
  there, not through tool-specific adapters.

## Tool-Specific Notes

- Agents with access to `cc-plugin-codex` may request optional second-opinion
  review for security-sensitive, MCP-contract, release, or compatibility changes,
  but it is a paid external call. Do not send secrets or sensitive workspace
  contents to Claude.
- For Claude Code compatibility, `.claude/skills` may be a local or committed
  symlink to `.agents/skills`. Do not commit machine-local `.claude` state.

## Security Reports

Follow `SECURITY.md` for vulnerability handling. Do not open public issues or PRs
with exploit details, secrets, or private workspace contents.
