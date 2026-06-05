import subprocess

import pytest

from cc_plugin_codex.context import (
    ContextResult,
    InvalidBaseError,
    InvalidScopeError,
    _diff_args,
    gather_context,
)


def test_working_tree_diff(git_repo):
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert isinstance(res, ContextResult)
    assert "a - b" in res.text
    assert res.summary.files_changed == 1
    assert res.summary.lines_added >= 1
    assert res.truncated is False
    assert res.diff_bytes == len(res.text.encode("utf-8"))


def test_diff_bytes_reports_full_size_when_truncated(git_repo, monkeypatch):
    import cc_plugin_codex.context as ctx

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


def test_secret_values_in_source_are_redacted(git_repo):
    (git_repo / "app.py").write_text(
        "def add(a, b):\n    token = 'ghp_1234567890abcdefghijklmnopqrstu'\n    return a - b\n"
    )
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert "ghp_1234567890abcdefghijklmnopqrstu" not in res.text
    assert "[redacted: secret value]" in res.text
    assert "app.py" in res.redacted_paths


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


def test_git_timeout_is_bounded(monkeypatch, git_repo):
    import cc_plugin_codex.context as ctx

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setenv("CC_PLUGIN_CODEX_GIT_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(ctx.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out after 2s"):
        gather_context(str(git_repo), scope="working_tree", base="main")


def test_size_cap_truncates(git_repo, monkeypatch):
    import cc_plugin_codex.context as ctx

    monkeypatch.setattr(ctx, "MAX_DIFF_BYTES", 10)
    (git_repo / "big.py").write_text("x = 1\n" * 1000)
    subprocess.run(["git", "add", "-Nf", "big.py"], cwd=git_repo, check=True)
    res = gather_context(str(git_repo), scope="working_tree", base="main")
    assert res.truncated is True
    assert res.truncation_hint
    assert "scope=staged" in res.truncation_hint
    assert "review specific files" not in res.truncation_hint


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
    assert "--no-textconv" in _diff_args("working_tree", "main")
    assert "--no-textconv" in _diff_args("staged", "main")


def test_diff_args_bad_base_raises_invalid_base():
    with pytest.raises(InvalidBaseError):
        _diff_args("branch", "-badref")


def test_branch_base_rejects_nonexistent_ref(git_repo):
    with pytest.raises(InvalidBaseError):
        gather_context(str(git_repo), scope="branch", base="definitely-not-a-real-branch")


def test_diff_args_bad_scope_raises_invalid_scope():
    with pytest.raises(InvalidScopeError):
        _diff_args("nonsense", "main")
