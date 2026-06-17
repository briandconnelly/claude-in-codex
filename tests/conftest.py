"""Shared test fixtures and helpers."""

import json
import subprocess

import pytest


def structured(result):
    """Extract the structured payload from a FastMCP call result across versions."""
    data = getattr(result, "structured_content", None)
    if data is not None:
        return data
    return json.loads(result.content[0].text)


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway git repo with one committed file and one unstaged change."""

    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "Test")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    run("add", "app.py")
    run("commit", "-q", "-m", "init")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    return tmp_path


@pytest.fixture
def fake_claude(monkeypatch):
    """Replace server.run_claude_async so tests never invoke the real CLI or incur cost."""
    import cc_plugin_codex.server as srv
    from cc_plugin_codex.claude import ClaudeRun

    inner = {
        "summary": "off-by-one bug",
        "verdict": "concerns",
        "confidence": "high",
        "findings": [
            {
                "severity": "high",
                "title": "subtraction",
                "file": "app.py",
                "line": 2,
                "evidence": "a - b",
                "risk": "wrong result",
                "recommendation": "use +",
            }
        ],
        "questions": [],
        "assumptions": [],
    }
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(inner),
            "session_id": "sess-1",
            "modelUsage": {"claude-sonnet-4-6": {}},
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )

    async def fake_run(cmd, cwd, timeout_seconds, stdin_text=None, *, config_mode=None):
        return ClaudeRun(stdout=envelope, stderr="", exit_code=0, elapsed_ms=12, timed_out=False)

    monkeypatch.setattr(srv, "run_claude_async", fake_run)
    return envelope
