import subprocess

import pytest

from claude_in_codex.context import (
    ContextResult,
    DiffOptions,
    GitUnavailableError,
    InvalidBaseError,
    InvalidHeadError,
    InvalidPathsError,
    InvalidScopeError,
    NotAGitRepoError,
    _diff_args,
    gather_context,
)


def _current_branch(git_repo):
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_working_tree_diff(git_repo):
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert isinstance(res, ContextResult)
    assert "a - b" in res.text
    assert res.summary.files_changed == 1
    assert res.summary.lines_added >= 1
    assert res.truncated is False
    assert res.diff_bytes == len(res.text.encode("utf-8"))


def test_working_tree_diff_can_be_filtered_by_paths(git_repo):
    (git_repo / "other.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "-Nf", "other.py"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main", paths=["other.py"])
    assert "other.py" in res.text
    assert "app.py" not in res.text
    assert res.summary.files_changed == 1
    assert res.summary.lines_added == 1


def test_paths_none_and_empty_preserve_unfiltered_behavior(git_repo):
    unfiltered = gather_context(str(git_repo), scope="working_tree", base="main")
    none_paths = gather_context(str(git_repo), scope="working_tree", base="main", paths=None)
    empty_paths = gather_context(str(git_repo), scope="working_tree", base="main", paths=[])
    assert none_paths.text == unfiltered.text
    assert none_paths.summary == unfiltered.summary
    assert empty_paths.text == unfiltered.text
    assert empty_paths.summary == unfiltered.summary


@pytest.mark.parametrize(
    "path",
    [
        "",
        "-bad",
        "/tmp/file.py",
        "../x.py",
        "src/../x.py",
        ":!vendor",
        "C:/repo/file.py",
        "C:\\repo\\file.py",
        "\\\\server\\share\\file.py",
        "src\\..\\secret.py",
    ],
)
def test_invalid_paths_are_rejected_before_git(monkeypatch, git_repo, path):
    import claude_in_codex.context as ctx

    def fail_git(*_args, **_kwargs):
        raise AssertionError("git should not be called for invalid paths")

    monkeypatch.setattr(ctx, "_git", fail_git)
    with pytest.raises(InvalidPathsError):
        gather_context(str(git_repo), scope="working_tree", base="main", paths=[path])


def test_dotdot_substrings_are_valid_path_names(git_repo):
    path = git_repo / "foo..bar.py"
    path.write_text("value = 1\n")
    subprocess.run(["git", "add", "-Nf", path.name], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main", paths=[path.name])
    assert "foo..bar.py" in res.text


def test_diff_bytes_reports_full_size_when_truncated(git_repo, monkeypatch):
    import claude_in_codex.context as ctx

    monkeypatch.setattr(ctx, "MAX_DIFF_BYTES", 10)
    (git_repo / "big.py").write_text("x = 1\n" * 1000)
    subprocess.run(["git", "add", "-Nf", "big.py"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert res.truncated is True
    # diff_bytes is the true (pre-truncation) size, not the clipped text length.
    assert res.diff_bytes > 10
    assert len(res.text.encode("utf-8")) <= 10


def test_invalid_scope(git_repo):
    with pytest.raises(ValueError):
        gather_context(str(git_repo), scope="bogus", base="main")


def test_secret_files_redacted(git_repo):
    (git_repo / ".env").write_text("API_KEY=supersecret\n")
    # intent-to-add so the new file shows up in `git diff`
    subprocess.run(["git", "add", "-Nf", ".env"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "supersecret" not in res.text
    assert ".env" in res.text  # path noted as redacted
    assert ".env" in res.redacted_paths


@pytest.mark.parametrize(
    ("filename", "secret"),
    [
        (".netrc", "machine api.example.com login alice password supersecretpassword"),
        (".pypirc", "[pypi]\nusername = __token__\npassword = pypi-1234567890abcdefghijklmnop"),
        (".envrc", "export TOKEN=supersecretpassword123456"),
    ],
)
def test_common_credential_files_are_redacted(git_repo, filename, secret):
    (git_repo / filename).write_text(f"{secret}\n")
    subprocess.run(["git", "add", "-Nf", filename], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert secret not in res.text
    assert filename in res.text
    assert filename in res.redacted_paths


def test_secret_values_in_source_are_redacted(git_repo):
    (git_repo / "app.py").write_text(
        "def add(a, b):\n    token = 'ghp_1234567890abcdefghijklmnopqrstu'\n    return a - b\n"
    )
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "ghp_1234567890abcdefghijklmnopqrstu" not in res.text
    assert "[redacted: secret value]" in res.text
    assert "app.py" in res.redacted_paths


def test_password_style_values_in_source_are_redacted(git_repo):
    (git_repo / "config.ini").write_text(
        "password = supersecretpassword123456\n"
        "passwd = anothersecret12345678\n"
        "pwd = shortsecretvalue123456\n"
        "passphrase = sshkeypassphrase123456\n"
        "secret = shouldredact12345678\n"
    )
    subprocess.run(["git", "add", "-Nf", "config.ini"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "supersecretpassword123456" not in res.text
    assert "anothersecret12345678" not in res.text
    assert "shortsecretvalue123456" not in res.text
    assert "sshkeypassphrase123456" not in res.text
    assert "shouldredact12345678" not in res.text
    assert res.text.count("[redacted: secret value]") == 5
    assert "config.ini" in res.redacted_paths


def test_secret_values_in_removed_lines_are_redacted(git_repo):
    subprocess.run(["git", "checkout", "--", "app.py"], cwd=git_repo, check=True)
    (git_repo / "app.py").write_text(
        "def add(a, b):\n    token = 'ghp_1234567890abcdefghijklmnopqrstu'\n    return a + b\n"
    )
    subprocess.run(["git", "add", "app.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add token"], cwd=git_repo, check=True)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "ghp_1234567890abcdefghijklmnopqrstu" not in res.text
    assert "[redacted: secret value]" in res.text


def test_secret_values_in_context_lines_are_redacted(git_repo):
    subprocess.run(["git", "checkout", "--", "app.py"], cwd=git_repo, check=True)
    (git_repo / "app.py").write_text(
        "def add(a, b):\n    token = 'ghp_1234567890abcdefghijklmnopqrstu'\n    return a + b\n"
    )
    subprocess.run(["git", "add", "app.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add token"], cwd=git_repo, check=True)
    (git_repo / "app.py").write_text(
        "def add(a, b):\n    token = 'ghp_1234567890abcdefghijklmnopqrstu'\n    return a - b\n"
    )
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "ghp_1234567890abcdefghijklmnopqrstu" not in res.text
    assert "[redacted: secret value]" in res.text


def test_redacted_paths_are_normalized(git_repo):
    (git_repo / ".env").write_text("API_KEY=supersecret\n")
    subprocess.run(["git", "add", "-Nf", ".env"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert ".env" in res.redacted_paths
    assert all(" b/" not in path for path in res.redacted_paths)


def _secret(prefix: str, body: str) -> str:
    """Join a vendor prefix and a synthetic body at runtime.

    The full token never appears as one contiguous literal in source (the `+` is a
    runtime op the formatter won't collapse), so GitHub push protection and
    detect-secrets don't flag these fixtures — while the value the test scans is
    still the complete, credential-shaped token.
    """
    return prefix + body


# Each secret is synthetic and non-functional.
HIGH_CONFIDENCE_SECRETS = [
    pytest.param(
        _secret(
            "eyJ",
            "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        ),
        id="jwt",
    ),
    pytest.param(_secret("sk-proj-", "T3BlbkFJ1234567890abcdefghij"), id="openai-project"),
    pytest.param(_secret("sk-ant-", "api03-abcdefghij1234567890ABCD"), id="anthropic"),
    pytest.param(_secret("sk-", "T3BlbkFJ1234567890abcdefghijABCD"), id="openai-classic"),
    pytest.param(_secret("sk_live_", "51H8xqL2eZvKYlo2Cabcdefghij"), id="stripe-live"),
    pytest.param(_secret("sk_test_", "51H8xqL2eZvKYlo2Cabcdefghij"), id="stripe-test"),
    pytest.param(_secret("AIza", "SyB1234567890abcdefghijklmnopqrstuv"), id="google-api-key"),
    pytest.param(_secret("github_pat_", "11ABCDEFG0abcdefghijkl_l0123456789"), id="github-fg"),
    pytest.param(_secret("glpat-", "ABCdef1234567890123456"), id="gitlab-pat"),
    pytest.param(_secret("npm_", "0123456789abcdefghijklmnopqrstuvwxyz"), id="npm-token"),
    pytest.param(_secret("pypi-", "AgEIcHlwaS5vcmcabcdefghij"), id="pypi-token"),
]


@pytest.mark.parametrize("secret", HIGH_CONFIDENCE_SECRETS)
def test_high_confidence_secret_prefixes_are_redacted(git_repo, secret):
    # Embed as a bare call argument (no key=value keyword) so only the prefix
    # pattern can catch it, not the generic assignment pattern.
    (git_repo / "leak.py").write_text(f'connect("{secret}")\n')
    subprocess.run(["git", "add", "-Nf", "leak.py"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert secret not in res.text
    assert "[redacted: secret value]" in res.text
    assert "leak.py" in res.redacted_paths


def test_connection_string_password_is_redacted(git_repo):
    (git_repo / "settings.py").write_text(
        'DATABASE_URL = "postgres://admin:s3cretP4ssw0rd@db.example.com:5432/app"\n'
    )
    subprocess.run(["git", "add", "-Nf", "settings.py"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "s3cretP4ssw0rd" not in res.text
    # Non-secret userinfo and host stay visible so the diff is still reviewable.
    assert "admin" in res.text
    assert "db.example.com" in res.text
    assert "settings.py" in res.redacted_paths


# An OpenSSH/PEM/PGP block embedded in an ordinary (non-secret-path) file: the
# whole base64 body must drop, not just the BEGIN marker line.
PRIVATE_KEY_BLOCKS = [
    pytest.param(
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----END OPENSSH PRIVATE KEY-----",
        id="openssh",
    ),
    pytest.param(
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----END RSA PRIVATE KEY-----",
        id="rsa-pem",
    ),
    pytest.param(
        "-----BEGIN PRIVATE KEY-----",
        "-----END PRIVATE KEY-----",
        id="pkcs8",
    ),
    pytest.param(
        "-----BEGIN PGP PRIVATE KEY BLOCK-----",
        "-----END PGP PRIVATE KEY BLOCK-----",
        id="pgp",
    ),
]


@pytest.mark.parametrize(("begin", "end"), PRIVATE_KEY_BLOCKS)
def test_inline_private_key_block_body_is_redacted(git_repo, begin, end):
    body_marker = "MIIEvQIBADANBgkqSECRETKEYBODYdeadbeef0123456789"
    (git_repo / "config.py").write_text(
        f'PEM = """\n{begin}\n{body_marker}\n{body_marker}line2\n{end}\n"""\n'
    )
    subprocess.run(["git", "add", "-Nf", "config.py"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert body_marker not in res.text
    assert "[redacted: secret value]" in res.text
    # Markers stay visible so a reviewer can see a key was present.
    assert begin in res.text
    assert end in res.text
    assert "config.py" in res.redacted_paths


def test_escaped_single_line_private_key_is_redacted(git_repo):
    # An escaped one-line key (BEGIN, body, and END on a single physical line, as
    # seen in .env/JSON/CI configs). The body must drop and the block must not
    # bleed onto the following, unrelated line.
    body = "MIIEvQIBADANBgkqSECRETKEYBODYdeadbeef0123456789"
    (git_repo / "config.json").write_text(
        "{\n"
        f'  "key": "-----BEGIN PRIVATE KEY-----\\n{body}\\n-----END PRIVATE KEY-----\\n",\n'
        '  "after": "not-a-secret-value-here"\n'
        "}\n"
    )
    subprocess.run(["git", "add", "-Nf", "config.json"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert body not in res.text
    assert "[redacted: secret value]" in res.text
    assert "-----BEGIN PRIVATE KEY-----" in res.text
    assert "-----END PRIVATE KEY-----" in res.text
    # The unrelated following line survives: state did not leak past the one-liner.
    assert "not-a-secret-value-here" in res.text
    assert "config.json" in res.redacted_paths


def test_ordinary_code_is_not_over_redacted(git_repo):
    (git_repo / "ordinary.py").write_text(
        "sky_is_blue = True\n"
        'ai_model = "fast"\n'
        "risky = compute()\n"
        "skip = False\n"
        "glpatch = apply(diff)\n"
        'homepage = "https://example.com/path"\n'
        'greeting = "eyJhbGci is not a token here"\n'
    )
    subprocess.run(["git", "add", "-Nf", "ordinary.py"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "[redacted" not in res.text
    assert "ordinary.py" not in res.redacted_paths


def test_git_timeout_is_bounded(monkeypatch, git_repo):
    import claude_in_codex.context as ctx

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setenv("CLAUDE_IN_CODEX_GIT_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(ctx.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out after 2s"):
        gather_context(str(git_repo), scope="working_tree", base="main")


def test_git_invocations_force_c_locale(monkeypatch, git_repo):
    import claude_in_codex.context as ctx

    envs = []

    def fake_run(*args, **kwargs):
        envs.append(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setenv("PATH", "preserved-path")
    monkeypatch.setattr(ctx.subprocess, "run", fake_run)
    gather_context(str(git_repo), scope="working_tree", base="main")
    assert envs
    assert all(env["LC_ALL"] == "C" for env in envs)
    assert all(env["LANG"] == "C" for env in envs)
    assert all(env["PATH"] == "preserved-path" for env in envs)


def test_non_git_working_tree_raises_not_a_git_repo(tmp_path):
    with pytest.raises(NotAGitRepoError):
        gather_context(str(tmp_path), scope="working_tree", base="main")


def test_non_git_branch_scope_raises_not_a_git_repo(tmp_path):
    with pytest.raises(NotAGitRepoError):
        gather_context(str(tmp_path), scope="branch", base="main")


def test_missing_git_raises_git_unavailable(monkeypatch, git_repo):
    import claude_in_codex.context as ctx

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(ctx.subprocess, "run", fake_run)
    with pytest.raises(GitUnavailableError):
        gather_context(str(git_repo), scope="working_tree", base="main")


def test_missing_git_for_branch_scope_raises_git_unavailable(monkeypatch, git_repo):
    import claude_in_codex.context as ctx

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(ctx.subprocess, "run", fake_run)
    with pytest.raises(GitUnavailableError):
        gather_context(str(git_repo), scope="branch", base="main")


def test_size_cap_truncates(git_repo, monkeypatch):
    import claude_in_codex.context as ctx

    monkeypatch.setattr(ctx, "MAX_DIFF_BYTES", 10)
    (git_repo / "big.py").write_text("x = 1\n" * 1000)
    subprocess.run(["git", "add", "-Nf", "big.py"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert res.truncated is True
    assert res.truncation_hint
    assert "paths=[...]" in res.truncation_hint
    assert "scope=staged" in res.truncation_hint
    assert "review specific files" not in res.truncation_hint


def test_filtered_small_file_avoids_large_unfiltered_truncation(git_repo, monkeypatch):
    import claude_in_codex.context as ctx

    monkeypatch.setattr(ctx, "MAX_DIFF_BYTES", 500)
    (git_repo / "big.py").write_text("x = 1\n" * 1000)
    (git_repo / "small.py").write_text("ok = True\n")
    subprocess.run(["git", "add", "-Nf", "big.py", "small.py"], cwd=git_repo, check=True)
    unfiltered = gather_context(str(git_repo), scope="working_tree", base="main")
    filtered = gather_context(str(git_repo), scope="working_tree", base="main", paths=["small.py"])
    assert unfiltered.truncated is True
    assert filtered.truncated is False
    assert "small.py" in filtered.text
    assert "big.py" not in filtered.text


def test_stage_env_file_redacted(git_repo):
    (git_repo / "prod.env").write_text("DB_PASSWORD=hunter2\n")
    subprocess.run(["git", "add", "-Nf", "prod.env"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "hunter2" not in res.text
    assert "prod.env" in res.text


def test_branch_base_rejects_option_like_ref(git_repo):
    with pytest.raises(ValueError):
        gather_context(str(git_repo), scope="branch", base="--output=/tmp/pwn")


def test_pem_file_redacted(git_repo):
    (git_repo / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nDEADBEEF\n")
    subprocess.run(["git", "add", "-Nf", "server.pem"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "DEADBEEF" not in res.text
    assert "server.pem" in res.text


def test_diff_args_include_no_textconv():
    assert "--no-textconv" in _diff_args(DiffOptions("working_tree", "main"))
    assert "--no-textconv" in _diff_args(DiffOptions("staged", "main"))


def test_diff_args_bad_base_raises_invalid_base():
    with pytest.raises(InvalidBaseError):
        _diff_args(DiffOptions("branch", "-badref"))


def test_branch_base_rejects_nonexistent_ref(git_repo):
    with pytest.raises(InvalidBaseError):
        gather_context(str(git_repo), scope="branch", base="definitely-not-a-real-branch")


def test_branch_scope_diff_summarizes_valid_branch(git_repo):
    base = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "switch", "-c", "feature"], cwd=git_repo, check=True)
    (git_repo / "branch.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "branch.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "branch change"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="branch", base=base)
    assert "branch.py" in res.text
    assert res.summary.files_changed == 1
    assert res.summary.lines_added == 1


def test_branch_scope_paths_filter_diff_and_numstat(git_repo):
    base = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "switch", "-c", "feature"], cwd=git_repo, check=True)
    (git_repo / "src").mkdir()
    (git_repo / "docs").mkdir()
    (git_repo / "src" / "feature.py").write_text("value = 1\n")
    (git_repo / "docs" / "note.md").write_text("note\n")
    subprocess.run(["git", "add", "src/feature.py", "docs/note.md"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "branch changes"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="branch", base=base, paths=["src"])
    assert "src/feature.py" in res.text
    assert "docs/note.md" not in res.text
    assert res.summary.files_changed == 1
    assert res.summary.lines_added == 1


def test_diff_args_bad_scope_raises_invalid_scope():
    with pytest.raises(InvalidScopeError):
        _diff_args(DiffOptions("nonsense", "main"))


def test_diff_args_uses_explicit_head_range():
    args = _diff_args(DiffOptions("branch", "main", head="feature"))
    assert "main...feature" in args
    assert "main...HEAD" not in args


def test_diff_args_default_head_preserves_head_range():
    args = _diff_args(DiffOptions("branch", "main"))
    assert "main...HEAD" in args


def test_diff_args_malformed_head_raises_invalid_head():
    with pytest.raises(InvalidHeadError):
        _diff_args(DiffOptions("branch", "main", head="--output=/tmp/pwn"))


def test_branch_head_rejects_nonexistent_ref(git_repo):
    base = _current_branch(git_repo)
    with pytest.raises(InvalidHeadError):
        gather_context(str(git_repo), scope="branch", base=base, head="not-a-real-ref")


def test_branch_empty_string_head_is_rejected_not_coalesced(git_repo):
    # An explicit "" must fail validation rather than silently defaulting to HEAD.
    base = _current_branch(git_repo)
    with pytest.raises(InvalidHeadError):
        gather_context(str(git_repo), scope="branch", base=base, head="")


def test_branch_explicit_branch_head_works(git_repo):
    base = _current_branch(git_repo)
    subprocess.run(["git", "switch", "-c", "feature"], cwd=git_repo, check=True)
    (git_repo / "branch.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "branch.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "branch change"], cwd=git_repo, check=True)
    subprocess.run(["git", "switch", base], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="branch", base=base, head="feature")
    assert "branch.py" in res.text
    assert res.summary.files_changed == 1


def test_branch_explicit_commit_head_works(git_repo):
    base = _current_branch(git_repo)
    subprocess.run(["git", "switch", "-c", "feature"], cwd=git_repo, check=True)
    (git_repo / "branch.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "branch.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "branch change"], cwd=git_repo, check=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "switch", base], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="branch", base=base, head=head_sha)
    assert "branch.py" in res.text


def test_branch_paths_filter_with_explicit_head(git_repo):
    base = _current_branch(git_repo)
    subprocess.run(["git", "switch", "-c", "feature"], cwd=git_repo, check=True)
    (git_repo / "src").mkdir()
    (git_repo / "docs").mkdir()
    (git_repo / "src" / "feature.py").write_text("value = 1\n")
    (git_repo / "docs" / "note.md").write_text("note\n")
    subprocess.run(["git", "add", "src/feature.py", "docs/note.md"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "branch changes"], cwd=git_repo, check=True)
    subprocess.run(["git", "switch", base], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="branch", base=base, head="feature", paths=["src"])
    assert "src/feature.py" in res.text
    assert "docs/note.md" not in res.text
    assert res.summary.files_changed == 1


@pytest.mark.parametrize("scope", ["working_tree", "staged"])
def test_explicit_head_rejected_for_non_branch_scope(git_repo, scope):
    with pytest.raises(InvalidHeadError):
        gather_context(str(git_repo), scope=scope, base="main", head="feature")
