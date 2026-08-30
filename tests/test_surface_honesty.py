"""The agent-visible surface must not contradict `cli_contract.py`.

Adopted from the sibling bridges via the shared pontonier test kit.
Description-only defects pass every other gate — the code is right and the
prose is wrong — so these tests read the BUILT surface (the same one the
fingerprint digest covers), never source text. The phrase bans live in
`cli_contract.FORBIDDEN_SURFACE_PHRASES`, next to the facts that justify
them; the conformance check also pins the declarative `PONTONIER_CONTRACT`'s
derivation from the legacy constants so the two can never drift.
"""

from __future__ import annotations

import json

import pytest
from pontonier.testing import conformance, surface_honesty

from claude_in_codex import cli_contract
from test_fingerprint import _contract_surface


@pytest.fixture(scope="module")
def wire_text() -> str:
    """The full agent-visible surface, as the fingerprint digest covers it."""
    import asyncio

    return json.dumps(asyncio.run(_contract_surface()), ensure_ascii=False)


@pytest.mark.parametrize("phrase", cli_contract.FORBIDDEN_SURFACE_PHRASES)
def test_wire_prose_does_not_contradict_the_cli_contract(wire_text: str, phrase: str):
    assert surface_honesty.find_forbidden_phrases(wire_text, (phrase,)) == [], (
        f"{phrase!r} appears in the agent-visible surface. Cross-bridge vocabulary "
        "(codex exec / kimi / moonbridge) means a wrong-direction backport reached "
        "the wire; a mechanism claim (a sandbox, applying diffs) contradicts what "
        "this review-only server actually does."
    )


def test_contract_passes_pontonier_conformance():
    assert conformance.check_contract(cli_contract.PONTONIER_CONTRACT) == []


def test_contract_instance_derives_from_legacy_constants():
    """The declarative PONTONIER_CONTRACT and the constants claude.py still consumes
    are the same facts in two shapes; pin the derivation so they cannot drift."""
    c = cli_contract.PONTONIER_CONTRACT
    assert c.bin_name == cli_contract.CLAUDE_BIN
    assert c.exec_argv_prefix == cli_contract.CORE_INVOCATION
    assert set(c.always_send_flags) == set(cli_contract.ALWAYS_SEND_FLAGS)
    assert set(c.help_gated_flags) == set(cli_contract.HELP_GATED_FLAGS)
    assert set(c.usage_event_markers) == set(cli_contract.USAGE_KEYS)
    assert len(c.failure_signatures.contract_drift) == len(
        cli_contract.CONTRACT_DRIFT_STDERR_PATTERNS
    )
    # Review-only: no delegate, no transfer, no sessions.
    assert "delegate" not in c.supported_features
    assert "transfer" not in c.supported_features
    assert "sessions" not in c.supported_features


def test_signature_regexes_match_what_the_predicates_match():
    """The escaped, case-insensitive regex forms classify the same evidence the
    legacy predicates do — the shared classifier must not weaken classification
    when the adapter migration lands."""
    import re

    drift_sample = "error: unknown option '--zap'"
    sigs = cli_contract.PONTONIER_CONTRACT.failure_signatures
    assert cli_contract.is_contract_drift(drift_sample)
    assert any(re.search(p, drift_sample) for p in sigs.contract_drift)
    auth_sample = "Error: Not logged in"
    from claude_in_codex.claude import _has_logged_out_signal

    assert _has_logged_out_signal(auth_sample.lower())
    assert any(re.search(p, auth_sample) for p in sigs.auth)


def test_forbidden_phrases_are_still_justified():
    """Guard the guard: each ban exists because of a fact that can change. If this
    server ever gains a delegate tier or a real sandbox, retire the ban rather
    than leave a stale prohibition."""
    # Review-only: the access flags grant at most Read/Grep/Glob — nothing writes.
    assert "--tools" in cli_contract.ALWAYS_SEND_FLAGS
    assert cli_contract.PONTONIER_CONTRACT.backend_id == "claude"


def test_extra_args_policy_declares_no_operator_form():
    """The declarative half must match the behavior half. This server exposes no
    operator extra-args channel, and an empty policy is what says so: every
    descriptor is refused loudly. The caller's persona is not an extra arg — it
    rides RunRequest.instructions_append — so no form is declared for it."""
    policy = cli_contract.PONTONIER_CONTRACT.extra_args
    assert policy.allowed_option_forms == ()


def test_extra_args_policy_reserves_first_class_parameter_keys():
    """extra_args must not be able to shadow parameters the tools own."""
    reserved = cli_contract.PONTONIER_CONTRACT.extra_args.reserved_keys
    assert {"model", "effort"} <= reserved
