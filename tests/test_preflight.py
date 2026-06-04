"""Tests for the `claude --help` flag feature-detection (preflight)."""

from cc_plugin_codex import cli_contract, preflight
from cc_plugin_codex.preflight import FlagSupport


def test_parse_supported_extracts_long_flags():
    help_text = "Usage: claude -p [options]\n  --effort <level>\n  --no-chrome\n  -m, --model"
    supported = preflight._parse_supported(help_text)
    assert "--effort" in supported
    assert "--no-chrome" in supported
    assert "--model" in supported


def test_flag_support_fails_open_when_probe_empty(monkeypatch):
    # Probe failed (claude missing / timed out) -> help_parsed False -> everything
    # is treated as supported, preserving today's behavior.
    monkeypatch.setattr(preflight, "_probe_help", lambda: "")
    preflight.reset_cache()
    fs = preflight.flag_support(force=True)
    assert fs.help_parsed is False
    assert preflight.is_supported("--effort", fs) is True
    assert preflight.is_supported("--anything", fs) is True


def test_is_supported_when_help_parsed():
    fs = FlagSupport(supported=frozenset({"--effort"}), help_parsed=True)
    assert preflight.is_supported("--effort", fs) is True
    assert preflight.is_supported("--model", fs) is False


def test_flag_support_caches(monkeypatch):
    calls = {"n": 0}

    def _probe():
        calls["n"] += 1
        return "--effort\n--model"

    monkeypatch.setattr(preflight, "_probe_help", _probe)
    preflight.reset_cache()
    preflight.flag_support(force=True)
    preflight.flag_support()  # cached -> no second probe
    assert calls["n"] == 1


def test_missing_expected_flags_reports_absent_always_send():
    # A guarantee-bearing flag absent from a *successful* probe is reported as a
    # drift signal.
    present = frozenset(cli_contract.ALWAYS_SEND_FLAGS) - {"--tools"}
    fs = FlagSupport(supported=present, help_parsed=True)
    assert "--tools" in preflight.missing_expected_flags(fs)


def test_missing_expected_flags_empty_when_probe_failed():
    fs = FlagSupport(supported=frozenset(), help_parsed=False)
    assert preflight.missing_expected_flags(fs) == []
