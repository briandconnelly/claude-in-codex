"""Tests for the no-spend `scripts/check_claude_contract.py` drift check.

The script lives outside the package (it is a CI lint tool, not shipped code), so
it is loaded by path. Every test stubs the `subprocess.run` it uses, so the real
`claude` CLI is never invoked and no run can spend.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

from claude_in_codex import cli_contract

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_claude_contract.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_claude_contract", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check = _load_script()


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _full_help():
    """A help blob listing every flag the contract cares about, plus -p."""
    lines = ["Usage: claude [options] [prompt]", "  -p, --print", "  --help", "  --version"]
    lines += [f"  {flag} <value>" for flag in cli_contract.ALWAYS_SEND_FLAGS]
    lines += [f"  {flag} <value>" for flag in cli_contract.HELP_GATED_FLAGS]
    return "\n".join(lines) + "\n"


def _make_runner(
    *,
    version="2.1.0 (Claude Code)",
    help_text=None,
    bogus_stderr="unknown option",
    bogus_returncode=1,
):
    """Build a fake subprocess.run dispatching on the args it receives."""
    help_text = _full_help() if help_text is None else help_text

    def fake_run(cmd, **_kwargs):
        args = cmd[1:]
        if args == list(cli_contract.VERSION_ARGS):
            return _completed(stdout=version)
        if args == list(cli_contract.HELP_ARGS):
            return _completed(stdout=help_text)
        # The bogus-flag drift-signature probe.
        return _completed(stderr=bogus_stderr, returncode=bogus_returncode)

    return fake_run


def _patch(monkeypatch, runner):
    monkeypatch.setattr(check.subprocess, "run", runner)


def test_passes_when_contract_holds(monkeypatch, capsys):
    _patch(monkeypatch, _make_runner())
    assert check.main() == 0
    out = capsys.readouterr().out
    assert "contract holds" in out
    assert "FAIL" not in out


def test_missing_always_send_flag_is_drift(monkeypatch, capsys):
    one = next(iter(cli_contract.ALWAYS_SEND_FLAGS))
    help_text = _full_help().replace(f"  {one} <value>\n", "")
    _patch(monkeypatch, _make_runner(help_text=help_text))
    assert check.main() == 1
    assert one in capsys.readouterr().out


def test_missing_core_invocation_is_drift(monkeypatch, capsys):
    # Drop both -p/--print and --output-format from the help text.
    help_text = (
        _full_help().replace("  -p, --print\n", "").replace("  --output-format <value>\n", "")
    )
    _patch(monkeypatch, _make_runner(help_text=help_text))
    assert check.main() == 1
    out = capsys.readouterr().out
    assert "core flag --output-format absent" in out
    assert "core print mode" in out


def test_absent_help_gated_flag_is_only_warning(monkeypatch, capsys):
    help_text = _full_help().replace("  --effort <value>\n", "")
    _patch(monkeypatch, _make_runner(help_text=help_text))
    assert check.main() == 0
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "--effort absent" in out


def test_unsupported_major_warns_not_fails(monkeypatch, capsys):
    _patch(monkeypatch, _make_runner(version="99.0.0 (Claude Code)"))
    assert check.main() == 0
    assert "NOT in SUPPORTED_MAJORS" in capsys.readouterr().out


def test_binary_missing_returns_probe_failure(monkeypatch, capsys):
    def boom(cmd, **_kwargs):
        raise FileNotFoundError("claude")

    _patch(monkeypatch, boom)
    assert check.main() == 2
    assert "could not probe" in capsys.readouterr().out


def test_timeout_returns_probe_failure(monkeypatch):
    def slow(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, 10)

    _patch(monkeypatch, slow)
    assert check.main() == 2


def test_unparseable_help_is_probe_failure(monkeypatch, capsys):
    # Non-empty help, but no recognizable long flags (format/parser drift).
    _patch(monkeypatch, _make_runner(help_text="some banner with no flags at all\n"))
    assert check.main() == 2
    assert "no recognizable long flags" in capsys.readouterr().out


def test_reworded_drift_signature_is_only_warning(monkeypatch, capsys):
    _patch(monkeypatch, _make_runner(bogus_stderr="that argument is not allowed here"))
    assert check.main() == 0
    out = capsys.readouterr().out
    assert "NOT rejected with a recognized drift signature" in out
    assert "contract holds" in out


def test_accepted_unknown_flag_warns_even_with_matching_text(monkeypatch, capsys):
    # A zero-exit probe means the unknown flag was accepted/ignored, not rejected;
    # matching stderr text must NOT be reported as OK (the guarantee is rejection).
    _patch(monkeypatch, _make_runner(bogus_stderr="unknown option", bogus_returncode=0))
    assert check.main() == 0
    out = capsys.readouterr().out
    assert "NOT rejected with a recognized drift signature" in out


@pytest.mark.parametrize("code", [0, 1, 2])
def test_exit_codes_are_returned_verbatim(monkeypatch, code):
    # Smoke-check the documented 0/1/2 scheme is wired through main()'s return.
    if code == 0:
        _patch(monkeypatch, _make_runner())
    elif code == 1:
        help_text = _full_help().replace("  --tools <value>\n", "")
        _patch(monkeypatch, _make_runner(help_text=help_text))
    else:
        _patch(monkeypatch, _make_runner(help_text="no flags here\n"))
    assert check.main() == code
