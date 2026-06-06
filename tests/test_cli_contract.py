"""Canary tests for the centralized CLI contract.

These guard against a careless edit silently emptying a constant set or letting
config.py emit a flag the contract does not classify."""

from cc_plugin_codex import cli_contract
from cc_plugin_codex.config import access_flags, config_mode_flags


def test_core_and_effort_constants_present():
    assert cli_contract.CLAUDE_BIN == "claude"
    assert "-p" in cli_contract.CORE_INVOCATION
    assert "json" in cli_contract.CORE_INVOCATION
    assert "xhigh" in cli_contract.VALID_EFFORTS
    assert cli_contract.DEFAULT_EFFORT in cli_contract.VALID_EFFORTS
    assert cli_contract.SUPPORTED_MAJORS  # non-empty


def test_flag_classes_are_disjoint_and_nonempty():
    assert cli_contract.ALWAYS_SEND_FLAGS
    assert cli_contract.HELP_GATED_FLAGS
    assert not (cli_contract.ALWAYS_SEND_FLAGS & set(cli_contract.HELP_GATED_FLAGS))


def test_security_flags_are_always_send():
    # Losing any of these silently would weaken a security/cost/behavioral
    # guarantee, so they must never be in the help-gated (droppable) class.
    for flag in (
        "--tools",
        "--strict-mcp-config",
        "--mcp-config",
        "--max-budget-usd",
        "--no-session-persistence",
        "--append-system-prompt",
    ):
        assert flag in cli_contract.ALWAYS_SEND_FLAGS
        assert flag not in cli_contract.HELP_GATED_FLAGS


def test_every_emitted_flag_is_classified():
    # Whatever config.py actually emits must be a flag the contract knows about, so
    # the gate and the diagnostic never miss a real flag. (Skips non-flag tokens
    # like values and the empty --tools argument.)
    known = set(cli_contract.ALWAYS_SEND_FLAGS) | set(cli_contract.HELP_GATED_FLAGS)
    emitted = []
    for mode in ("inherit", "scoped", "bare"):
        emitted += config_mode_flags(mode)
    for access in ("toolless", "readonly"):
        emitted += access_flags(access)
    for token in emitted:
        if token.startswith("--"):
            assert token in known, f"{token} is emitted but not classified in cli_contract"


def test_is_contract_drift_matches_known_phrasings():
    assert cli_contract.is_contract_drift("error: unknown option '--effort'")
    assert cli_contract.is_contract_drift("invalid choice: 'xhigh'")
    assert cli_contract.is_contract_drift(None, "Unrecognized argument")  # case-insensitive
    assert not cli_contract.is_contract_drift("not logged in")
    assert not cli_contract.is_contract_drift(None, None)
