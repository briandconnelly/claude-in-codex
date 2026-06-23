#!/usr/bin/env python
"""No-spend drift check between the installed `claude` CLI and our cli_contract.

Runs only the two FREE local probes the plugin already uses — `claude --version`
and `claude --help` — plus one zero-spend argument-rejection probe, and diffs what
they report against the source-of-truth constants in
``claude_in_codex.cli_contract``. No `-p` print run, no model call, no token spend.

It reuses the SAME parser the running server uses (``preflight._parse_supported``)
and the same version gate (``config.version_supported``), so a flag this script
cannot find is a flag the server's feature-detection cannot find either. It is the
mechanical half of the upgrade procedure in ``COMPATIBILITY.md``; the judgment half
(do flag *semantics* still hold? did the JSON envelope change?) cannot be automated
from ``--help`` and stays covered by the manual checklist plus the golden-envelope
fixture test (``tests/test_golden_envelope.py``).

Coverage gap, by design: the JSON envelope keys / success subtypes / usage keys
(``ENVELOPE_KEYS`` / ``SUCCESS_SUBTYPES`` / ``USAGE_KEYS``) cannot be observed
without a paid ``claude -p --output-format json`` run, so they are NOT asserted
live here. They remain checked no-spend by the golden-envelope fixture test.

Usage:
    uv run python scripts/check_claude_contract.py

Exit codes:
    0  contract holds (warnings are non-fatal)
    1  drift: a guarantee-bearing flag or the core invocation is gone (a blocker)
    2  could not probe (claude missing / timed out / help unparseable) — nothing verified
"""

from __future__ import annotations

import re
import subprocess
import sys

from claude_in_codex import cli_contract, config, preflight

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"

_PROBE_TIMEOUT_SECONDS = 10
# A flag that cannot plausibly exist, used only to confirm `claude` still rejects
# unknown options (at arg-parse, before any model call — zero spend) AND that its
# rejection phrasing still matches one of CONTRACT_DRIFT_STDERR_PATTERNS.
_BOGUS_FLAG = "--claude-in-codex-contract-probe-not-a-real-flag"


def _run(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run `claude <args>` capturing output; None on missing binary or timeout.

    stdin is closed (empty) so a probe can never block waiting for input."""
    try:
        return subprocess.run(
            [cli_contract.CLAUDE_BIN, *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _report_version(version_str: str) -> None:
    """Advisory: an untested major warns, never blocks."""
    supported = config.version_supported(version_str)
    majors = ", ".join(str(m) for m in sorted(cli_contract.SUPPORTED_MAJORS))
    if supported is None:
        print(f"{WARN}: could not parse MAJOR.MINOR.PATCH from {version_str!r}.")
    elif supported:
        print(f"{OK}: {version_str} -> major in SUPPORTED_MAJORS = {{{majors}}}.")
    else:
        print(
            f"{WARN}: {version_str} -> major NOT in SUPPORTED_MAJORS = {{{majors}}} "
            f"(untracked — bump SUPPORTED_MAJORS once verified)."
        )


def _check_core(flags: frozenset[str], help_text: str) -> bool:
    """Core invocation (-p / --output-format): the plugin cannot run without it.

    `--output-format` is a long flag (parsed); `-p` is short, so scan raw help.
    Returns True if anything blocking is missing."""
    blocking = False
    if "--output-format" in flags:
        print(f"{OK}: core flag --output-format present.")
    else:
        blocking = True
        print(f"{FAIL}: core flag --output-format absent from `claude --help`.")
    if re.search(r"(?:^|[\s,])-p\b", help_text) or "--print" in flags:
        print(f"{OK}: core print mode (-p/--print) present.")
    else:
        blocking = True
        print(f"{FAIL}: core print mode (-p/--print) absent from `claude --help`.")
    return blocking


def _check_flags(flags: frozenset[str]) -> bool:
    """ALWAYS_SEND misses block; HELP_GATED misses only warn. Returns blocking."""
    missing_always = sorted(f for f in cli_contract.ALWAYS_SEND_FLAGS if f not in flags)
    if missing_always:
        print(f"{FAIL}: ALWAYS_SEND flags absent from `claude --help`: {missing_always}")
        print("      These are sent unconditionally — a removal/rename weakens a guarantee.")
    else:
        print(f"{OK}: all {len(cli_contract.ALWAYS_SEND_FLAGS)} ALWAYS_SEND flags present.")
    for flag in sorted(cli_contract.HELP_GATED_FLAGS):
        if flag in flags:
            print(f"{OK}: HELP_GATED flag {flag} present.")
        else:
            print(f"{WARN}: HELP_GATED flag {flag} absent — server drops it gracefully.")
    return bool(missing_always)


def _check_drift_signature() -> None:
    """Advisory self-test of the unknown-flag rejection path.

    Confirms `claude` still rejects an unknown flag (proving the zero-spend
    arg-parse guarantee the whole plugin rests on) AND that its phrasing still
    matches a CONTRACT_DRIFT_STDERR_PATTERN, so classify_failure keeps labeling real
    drift as cli_contract_changed. A non-match is only a WARN: it may mean upstream
    reworded its error, which warrants a human look at the patterns."""
    bogus = _run(_BOGUS_FLAG)
    if bogus is None:
        print(f"{WARN}: drift-signature self-test could not run (probe missing/timed out).")
    elif bogus.returncode != 0 and cli_contract.is_contract_drift(bogus.stdout, bogus.stderr):
        # A NONZERO exit is required: the guarantee is that an unknown flag is
        # *rejected* before any model call. A zero exit with matching text would
        # mean the flag was accepted/ignored — not the guarantee we rely on.
        print(f"{OK}: unknown flag rejected with a recognized contract-drift signature.")
    else:
        print(
            f"{WARN}: unknown flag was NOT rejected with a recognized drift signature "
            f"(exit {bogus.returncode}). Review CONTRACT_DRIFT_STDERR_PATTERNS for reworded errors."
        )


def main() -> int:
    version_run = _run(*cli_contract.VERSION_ARGS)
    help_run = _run(*cli_contract.HELP_ARGS)

    version_str = version_run.stdout.strip() if version_run else ""
    help_text = f"{help_run.stdout}\n{help_run.stderr}" if help_run else ""

    if not version_str or not help_text.strip():
        print(f"{FAIL}: could not probe `claude` (binary missing, timed out, or empty output).")
        print("      Install/authenticate Claude Code, then re-run. Nothing was verified.")
        return 2

    flags = preflight._parse_supported(help_text)

    # A non-empty help blob that no longer yields even `--help` means the format (or
    # our parser) drifted. Report that as a probe failure, NOT as the removal of
    # every guarantee-bearing flag, so the message stays actionable.
    if "--help" not in flags:
        print(f"{FAIL}: parsed `claude --help` but found no recognizable long flags.")
        print("      The help format (or our parser) likely changed. Nothing verified.")
        return 2

    _report_version(version_str)
    blocking = _check_core(flags, help_text)
    blocking = _check_flags(flags) or blocking
    _check_drift_signature()

    print()
    if blocking:
        print(f"{FAIL}: contract drift detected — update cli_contract.py before shipping.")
        print("      See COMPATIBILITY.md for the assumption -> upstream-source map.")
        return 1
    print(f"{OK}: contract holds against {version_str}. Semantics still need manual checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
