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

## Workflows And Supply Chain

- Keep `GITHUB_TOKEN` permissions least-privilege.
- Pin third-party GitHub Actions to full commit SHAs.
- Do not add `pull_request_target` or `workflow_run` workflows unless the
  privileged/untrusted-input risks are explicitly reviewed.
- Bind untrusted GitHub event data through `env:` before using it in shell steps.
- Use `uv.lock` for dependency changes. Dependabot PRs go through the same review
  and CI requirements as other changes.
- Do not commit secrets, local credentials, generated caches, or local tool state.

## Security Reports

Follow `SECURITY.md` for vulnerability handling. Do not open public issues or PRs
with exploit details, secrets, or private workspace contents.
