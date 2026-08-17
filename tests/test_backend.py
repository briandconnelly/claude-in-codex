"""ClaudeBackend: real-adapter validation of the provisional pontonier protocol.

The load-bearing test is the argv differential: the adapter's PreparedRun must
build the SAME command `claude.build_command` builds for the tool paths. If the
two diverge, the adapter validates a protocol against behavior nobody runs.
"""

from __future__ import annotations

import json

import pytest
from pontonier.backend.protocol import AgentBackend, RunOutcome, RunRequest
from pontonier.core.runtime import CommandRun
from pontonier.testing import conformance

from claude_in_codex import backend as backend_mod
from claude_in_codex import claude, cli_contract, preflight
from claude_in_codex.cli_contract import PONTONIER_CONTRACT
from claude_in_codex.preflight import FlagSupport

BACKEND = backend_mod.ClaudeBackend()

_FULL_SUPPORT = FlagSupport(
    supported=frozenset({"--effort", "--model", "--disallowed-tools"}), help_parsed=True
)


@pytest.fixture(autouse=True)
def _stable_flag_support(monkeypatch):
    monkeypatch.setattr(preflight, "flag_support", lambda force=False: _FULL_SUPPORT)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip CLAUDE_IN_CODEX_* env so the adapter sees built-in defaults."""
    import os

    for key in list(os.environ):
        if key.startswith("CLAUDE_IN_CODEX_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_backend_is_structurally_conformant():
    assert isinstance(BACKEND, AgentBackend)


def test_backend_passes_pontonier_conformance():
    assert conformance.check_contract(PONTONIER_CONTRACT) == []
    assert conformance.check_backend(PONTONIER_CONTRACT, BACKEND) == []


async def test_prepared_argv_matches_production_builder(tmp_path, clean_env):
    """Differential: adapter argv == build_command argv, byte for byte."""
    request = RunRequest(
        kind="consult",
        prompt="why?",
        cwd=str(tmp_path),
        timeout_seconds=60,
        model="opus",
        reasoning_effort="high",
        budget_usd=1.0,
        config_mode="inherit",
        access="readonly",
    )
    async with BACKEND.prepare(request) as prepared:
        adapter_argv = list(prepared.argv)
        assert prepared.stdin_text == "why?"  # prompt over stdin, never argv
        assert prepared.artifacts == ()  # no file artifacts for this backend

    expected_cmd, dropped = claude.build_command(
        "why?", "inherit", "readonly", "opus", 1.0, effort="high", flag_support=_FULL_SUPPORT
    )
    assert dropped == []
    assert adapter_argv == expected_cmd


async def test_env_scrubbed_for_login_modes(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key")
    request = RunRequest(
        kind="consult", prompt="q", cwd=str(tmp_path), timeout_seconds=60, config_mode="inherit"
    )
    async with BACKEND.prepare(request) as prepared:
        assert "ANTHROPIC_API_KEY" not in prepared.env


async def test_bare_mode_keeps_credentials(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "needed-key")
    request = RunRequest(
        kind="consult", prompt="q", cwd=str(tmp_path), timeout_seconds=60, config_mode="bare"
    )
    async with BACKEND.prepare(request) as prepared:
        assert prepared.env.get("ANTHROPIC_API_KEY") == "needed-key"


def test_validate_request_enforces_valid_efforts():
    bad = RunRequest(
        kind="consult", prompt="q", cwd=".", timeout_seconds=10, reasoning_effort="ultra"
    )
    rejected = BACKEND.validate_request(bad)
    assert rejected is not None
    assert rejected.code == "invalid_reasoning_effort"
    for effort in cli_contract.VALID_EFFORTS:
        ok = RunRequest(
            kind="consult", prompt="q", cwd=".", timeout_seconds=10, reasoning_effort=effort
        )
        assert BACKEND.validate_request(ok) is None


def _outcome(stdout: str = "", stderr: str = "", exit_code: int = 0) -> RunOutcome:
    return RunOutcome(
        run=CommandRun(
            stdout=stdout, stderr=stderr, exit_code=exit_code, elapsed_ms=5, timed_out=False
        )
    )


def test_finalize_reads_the_stdout_envelope():
    request = RunRequest(kind="consult", prompt="q", cwd=".", timeout_seconds=10)
    envelope = json.dumps(
        {
            "result": "the answer",
            "total_cost_usd": 0.04,
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "session_id": "s-1",
            "subtype": "success",
        }
    )
    result = BACKEND.finalize(_outcome(stdout=envelope), request)
    assert result.answer == "the answer"
    assert result.usage.cost_usd == 0.04
    assert result.usage.input_tokens == 100
    assert result.session_id == "s-1"


def test_finalize_tolerates_garbage_stdout():
    request = RunRequest(kind="consult", prompt="q", cwd=".", timeout_seconds=10)
    result = BACKEND.finalize(_outcome(stdout="not json at all"), request)
    assert result.answer == ""
    assert result.usage is None


def test_classify_failure_delegates_and_is_config_mode_aware():
    """Same evidence in, same code out as claude.classify_failure — including the
    config_mode-dependent repair selection the production classifier does."""
    request = RunRequest(
        kind="consult", prompt="q", cwd=".", timeout_seconds=10, config_mode="inherit"
    )
    out = _outcome(stderr="Error: not logged in", exit_code=1)
    adapter_result = BACKEND.classify_failure(out, request)
    direct = claude.classify_failure(
        claude.ClaudeRun(
            stdout="", stderr="Error: not logged in", exit_code=1, elapsed_ms=5, timed_out=False
        ),
        config_mode="inherit",
    )
    assert adapter_result.code == direct.code == "claude_auth_required"


def test_classify_contract_drift():
    request = RunRequest(kind="consult", prompt="q", cwd=".", timeout_seconds=10)
    out = _outcome(stderr="error: unknown option '--zap'", exit_code=2)
    assert BACKEND.classify_failure(out, request).code == "cli_contract_changed"


def test_list_models_is_the_advisory_catalog():
    models = BACKEND.list_models()
    assert "opus" in models
    assert len(models) == len(cli_contract.KNOWN_MODELS)
