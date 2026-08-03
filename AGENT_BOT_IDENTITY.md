# Agent Bot Identity

Coding-agent sessions that write to this repository use the GitHub App identity
`briandconnelly-agent[bot]`. Manual shells and browser activity continue to use
the human's normal identity. This separation makes commits, pushes, issues, and
pull-request activity attributable without changing the machine's global git or
GitHub CLI login.

## Required Workflow

1. Start a fresh Codex or Claude Code session in the repository. Approve the
   repository's identity hook when the harness asks for hook trust.
2. Create or switch to a feature branch. Do not work directly on `main`.
3. Before the first GitHub write, verify both identity paths:

   ```sh
   git var GIT_AUTHOR_IDENT
   git var GIT_COMMITTER_IDENT
   gh api graphql -f 'query={viewer{login}}' --jq '.data.viewer.login'
   ```

   The git commands must report `briandconnelly-agent[bot]` with its GitHub
   noreply address, and the API command must report
   `briandconnelly-agent[bot]`.
4. Commit, push the branch, and open or update the pull request as the bot.
5. Let required checks run. Address review feedback on the branch, then request
   fresh human review after every post-review push.
6. Hand off an open, green pull request. A human reviews and merges it. An agent
   does not approve its own pull request or switch to a human identity to merge
   it.

If either preflight identity is absent or different, stop before writing to
GitHub. Do not run `gh auth login`, supply a human token, clear the command-scoped
git identity, or bypass a failed token mint.

## Local Routing

The local setup is intentionally outside the repository because it contains
machine-specific paths and credentials:

- `~/.claude/bot-shims/` contains the token minter, git credential helper, and
  harness adapters.
- `~/.config/briandconnelly-agent/key.pem` contains the GitHub App private key.
- `~/.cache/briandconnelly-agent/` contains short-lived, installation-keyed
  token caches.
- `~/.codex/config.toml` supplies Codex's bot environment and permission profile;
  `~/.codex/hooks.json` blocks identity drift before shell commands in this
  repository.
- `~/.claude/settings.json` installs Claude Code's SessionStart routing hook.
  The hook selects the bot only for repositories owned by a configured account
  and restores personal identity for non-mapped repositories.

The adapters fail closed: a missing helper, unavailable key, invalid
installation, or failed token mint must produce an authentication failure rather
than expose stored human credentials.

These controls route attribution and prevent common accidental identity drift;
they are not a complete operating-system security boundary. Work requiring hard
separation from every human credential should run under a dedicated OS account
or isolated container.

## Maintenance Checks

After changing the App installation, key, shims, or harness configuration:

1. Syntax-check the shell adapters and parse the JSON/TOML configuration.
2. Verify the Codex permission profile can write only the keyed token cache it
   needs and can reach the required GitHub endpoints.
3. Exercise Codex and Claude Code independently and confirm the GitHub actor.
4. Confirm a non-mapped repository restores personal identity.
5. Confirm the guard rejects missing bot identity and attempts to invoke the
   personal-identity escape path from an agent session.
6. Preserve a recovery copy before replacing local configuration, and retire
   obsolete unkeyed token caches after the keyed cache succeeds.
