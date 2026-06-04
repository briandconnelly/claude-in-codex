"""Build and run the `claude` CLI invocation; classify failures."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio

from cc_plugin_codex import cli_contract, preflight
from cc_plugin_codex.config import (
    INDEPENDENT_CRITIC_PROMPT,
    access_flags,
    config_mode_flags,
)
from cc_plugin_codex.schemas import ErrorInfo

if TYPE_CHECKING:
    from cc_plugin_codex.preflight import FlagSupport


@dataclass
class ClaudeRun:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_ms: int
    timed_out: bool


def _gate_optional(tokens: list[str], fs: FlagSupport) -> tuple[list[str], list[str]]:
    """Drop any HELP_GATED flag (and its value, if it takes one) the installed
    `claude` does not advertise in --help. Returns (kept_tokens, dropped_flags).
    ALWAYS_SEND flags are never in HELP_GATED_FLAGS, so they always survive."""
    kept: list[str] = []
    dropped: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        takes_value = cli_contract.HELP_GATED_FLAGS.get(token)
        if takes_value is not None and not preflight.is_supported(token, fs):
            dropped.append(token)
            i += 2 if takes_value else 1
            continue
        kept.append(token)
        i += 1
    return kept, dropped


def build_command(
    prompt: str,
    config_mode: str,
    access: str,
    model: str | None,
    max_budget_usd: float,
    effort: str | None = None,
    flag_support: FlagSupport | None = None,
) -> tuple[list[str], list[str]]:
    """Build the `claude` invocation. Returns (cmd, dropped_optional_flags).

    Guarantee-bearing flags are sent unconditionally; HELP_GATED (depth/cosmetic)
    flags are dropped when the installed CLI does not list them, so a minor
    upstream change degrades instead of aborting a paid run. dropped_optional_flags
    feeds Meta.compat_warnings."""
    fs = flag_support if flag_support is not None else preflight.flag_support()
    # --no-chrome disables the "Claude in Chrome" integration, which could
    # otherwise open an interactive picker that hangs an unattended run until the
    # timeout (burning the whole timeout and the spend) instead of answering.
    tokens = [cli_contract.CLAUDE_BIN, *cli_contract.CORE_INVOCATION, "--no-chrome"]
    tokens += config_mode_flags(config_mode)
    tokens += access_flags(access)
    tokens += ["--append-system-prompt", INDEPENDENT_CRITIC_PROMPT]
    tokens += ["--max-budget-usd", f"{max_budget_usd}"]
    if effort and effort in cli_contract.VALID_EFFORTS:
        tokens += ["--effort", effort]
    if model:
        tokens += ["--model", model]
    cmd, dropped = _gate_optional(tokens, fs)
    # Gate BEFORE appending the prompt so a prompt that contains "--effort" etc.
    # can never be mistaken for a flag.
    cmd += [cli_contract.END_OF_OPTIONS, prompt]
    return cmd, dropped


def auth_status(timeout_seconds: int = 10) -> tuple[bool | None, str | None]:
    """Probe `claude auth status` without making a paid call.

    Returns (logged_in, detail). logged_in is None when the probe could not run
    (claude missing, timeout) so callers can report 'unknown' rather than a
    misleading False. detail is a NON-identifying phrase, never the raw CLI output:
    `claude auth status` prints the account email and organization, which would leak
    into shared logs/transcripts. The boolean already carries the machine-readable
    truth, so we deliberately drop the raw text."""
    try:
        proc = subprocess.run(
            [cli_contract.CLAUDE_BIN, *cli_contract.AUTH_STATUS_ARGS],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    logged_in = proc.returncode == 0
    detail = (
        "Claude CLI reports an authenticated session."
        if logged_in
        else "Claude CLI reports no authenticated session; run `claude /login`."
    )
    return logged_in, detail


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort terminate the process and its children. POSIX: kill the
    process group (the child is its own session leader). Falls back to killing
    just the process where process groups are unavailable (e.g. Windows)."""
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX fallback
            proc.kill()
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def run_claude_async(cmd: list[str], cwd: str, timeout_seconds: int) -> ClaudeRun:
    """Run `claude` as a subprocess, returning a ClaudeRun.

    The subprocess is started in its own session (process group) so that, on a
    timeout OR an MCP request cancellation, we can terminate the whole tree
    rather than orphaning a paid Claude run."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError:
        elapsed = int((time.monotonic() - start) * 1000)
        return ClaudeRun("", "claude_not_found", 127, elapsed, False)

    def _wait() -> tuple[str, str, bool]:
        try:
            out, err = proc.communicate(timeout=timeout_seconds)
            return out, err, False
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            out, err = proc.communicate()
            return out, err, True

    try:
        out, err, timed_out = await anyio.to_thread.run_sync(_wait, abandon_on_cancel=True)
    except anyio.get_cancelled_exc_class():
        _kill_process_tree(proc)
        raise
    elapsed = int((time.monotonic() - start) * 1000)
    if timed_out:
        return ClaudeRun("", "timeout", -9, elapsed, True)
    return ClaudeRun(out, err, proc.returncode, elapsed, False)


def classify_failure(run: ClaudeRun) -> ErrorInfo:
    env = None
    with contextlib.suppress(json.JSONDecodeError, ValueError, TypeError):
        env = json.loads(run.stdout)
    if run.stderr == "claude_not_found":
        return ErrorInfo(
            code="claude_not_found",
            message="The `claude` CLI was not found on PATH.",
            repair="Install Claude Code and ensure `claude` is on PATH.",
        )
    if run.timed_out:
        return ErrorInfo(
            code="timeout",
            message="claude exceeded the timeout.",
            repair="Narrow the scope/focus or raise timeout_seconds.",
            retryable=True,
        )
    if isinstance(env, dict) and env.get("is_error"):
        subtype = str(env.get("subtype") or "").lower()
        result = str(env.get("result") or "")
        structured_blob = f"{subtype}\n{result}".lower()
        if "api_key" in structured_blob or "invalid api key" in structured_blob:
            return ErrorInfo(
                code="api_key_invalid",
                message="ANTHROPIC_API_KEY is invalid.",
                repair="Set a valid ANTHROPIC_API_KEY, or use config_mode "
                "inherit/scoped to use your existing login.",
            )
        if "auth" in structured_blob or "login" in structured_blob:
            return ErrorInfo(
                code="claude_auth_required",
                message="claude is not authenticated.",
                repair="Run `claude /login`.",
            )
        if "budget" in structured_blob:
            return ErrorInfo(
                code="budget_exceeded",
                message="claude reached the max-budget stop threshold "
                "(a best-effort limit, not a hard cap).",
                repair="Raise max_budget_usd or reduce context.",
                retryable=True,
            )
        if "permission" in structured_blob or "denied" in structured_blob:
            return ErrorInfo(
                code="claude_permission_error",
                message="claude was denied a requested permission.",
                repair="Use access=toolless, or allow the needed read-only tools.",
            )
        if "rate" in structured_blob or "overloaded" in structured_blob:
            return ErrorInfo(
                code="nonzero_exit",
                message=f"claude reported a retryable error: {result[:200]}",
                repair="Retry later, or reduce request size.",
                retryable=True,
            )

    extra = ""
    if isinstance(env, dict):
        extra = f"{env.get('subtype', '')} {env.get('result', '')}"
    blob = f"{extra}\n{run.stdout}\n{run.stderr}".lower()
    if "invalid api key" in blob:
        return ErrorInfo(
            code="api_key_invalid",
            message="ANTHROPIC_API_KEY is invalid.",
            repair="Set a valid ANTHROPIC_API_KEY, or use config_mode "
            "inherit/scoped to use your existing login.",
        )
    if "not logged in" in blob or "/login" in blob:
        return ErrorInfo(
            code="claude_auth_required",
            message="claude is not authenticated.",
            repair="Run `claude /login`.",
        )
    if "budget" in blob:
        return ErrorInfo(
            code="budget_exceeded",
            message="claude reached the max-budget stop threshold "
            "(a best-effort limit, not a hard cap).",
            repair="Raise max_budget_usd or reduce context.",
            retryable=True,
        )
    # An unknown flag / invalid value means the CLI contract drifted from what this
    # plugin sends. Check last so an auth/budget message is never misread as drift.
    if cli_contract.is_contract_drift(run.stderr, run.stdout):
        return contract_changed_error()
    return ErrorInfo(
        code="nonzero_exit",
        message=f"claude exited {run.exit_code}: {run.stderr.strip()[:200]}",
        repair="Inspect the error; retry with a smaller request.",
    )


def contract_changed_error() -> ErrorInfo:
    """Shared cli_contract_changed error, reused across every failure path so a
    drift is reported identically whether it surfaces on the sync, envelope, or
    async-job path."""
    return ErrorInfo(
        code="cli_contract_changed",
        message="claude rejected a flag or value this plugin sent — its CLI "
        "contract likely changed for your installed version.",
        repair="Update cc-plugin-codex (or pin claude to a supported version); "
        "run claude_status to check the version.",
    )
