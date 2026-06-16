"""Detached background jobs for long Claude reviews.

This server drives a one-shot ``claude -p --output-format json`` call, so a job's
terminal output is a single JSON envelope written to ``result.json`` — completion
is "the process exited and the envelope is present", with NO interactive-log or
TUI scraping. That makes background mode far simpler and more robust here than in
a harness that tails an interactive CLI.

State lives on disk (keyed by workspace), so status/result/cancel keep working
across MCP server restarts. There is no daemon: single-job lifecycle calls refresh
and TTL-clean the requested job, list calls clean the workspace, and the count cap
is enforced when jobs start. ``--max-budget-usd`` still applies its best-effort
spend stop threshold (not a hard cap) even for a job nobody polls.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from cc_plugin_codex.claude import contract_changed_error
from cc_plugin_codex.cli_contract import is_contract_drift
from cc_plugin_codex.normalize import apply_cost_usage, normalize_envelope
from cc_plugin_codex.schemas import (
    FINGERPRINT,
    ContextSummary,
    ErrorCode,
    ErrorInfo,
    ErrorResult,
    Meta,
    workspace_warning_for,
)

STATE_ENV = "CC_PLUGIN_CODEX_STATE_DIR"
TTL_ENV = "CC_PLUGIN_CODEX_JOB_TTL"
MAX_SECONDS_ENV = "CC_PLUGIN_CODEX_JOB_MAX_SECONDS"
MAX_COUNT_ENV = "CC_PLUGIN_CODEX_JOB_MAX_COUNT"

DEFAULT_TTL = 86_400  # delete terminal job records after 24h
DEFAULT_MAX_SECONDS = 1_800  # wall-clock cap; a poll past this reaps the job
DEFAULT_MAX_COUNT = 50  # retained jobs per workspace; evict oldest terminal

_TERMINAL = {"done", "failed", "cancelled", "timeout"}
_JOBS_LOCK = threading.RLock()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def max_seconds() -> int:
    return _int_env(MAX_SECONDS_ENV, DEFAULT_MAX_SECONDS)


def ttl_seconds() -> int:
    return _int_env(TTL_ENV, DEFAULT_TTL)


def poll_after_ms() -> int:
    return 1000


def _state_root() -> Path:
    root = os.environ.get(STATE_ENV)
    if root:
        return Path(root)
    return Path.home() / ".cache" / "cc-plugin-codex" / "jobs"


def _ws_dir(cwd: str) -> Path:
    canonical = os.path.realpath(cwd)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    # os.path.basename on the realpath string keeps the dir-name derivation stable
    # (and matches the digest input); Path.name differs on trailing-slash handling.
    base = os.path.basename(canonical.rstrip("/")) or "workspace"  # noqa: PTH119
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in base)[:40] or "ws"
    return _state_root() / f"{safe}-{digest}"


def _job_dir(cwd: str, job_id: str) -> Path:
    return _ws_dir(cwd) / job_id


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_running(pid: int | None) -> bool:
    """Whether the job process is still running.

    The job is launched detached but is still our child until it exits, so we
    must reap it with waitpid — otherwise it lingers as a zombie that kill(0)
    reports as 'alive' forever. waitpid(WNOHANG) returns (pid, _) once it exits
    (reaping it), (0, 0) while it runs, and raises ChildProcessError if it is not
    our child (e.g. after a server restart), where we fall back to a kill(0)
    liveness probe."""
    if not pid:
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
        if reaped == 0:
            return True
    except ChildProcessError:
        pass  # not our child — use the liveness probe below
    except OSError:
        return False
    return _pid_alive(pid)


def _kill_pid_tree(pid: int | None) -> None:
    """Kill the detached job's process group (it is its own session leader), then
    reap it if it was our child so it does not linger as a zombie."""
    if not pid:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX fallback
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, 0)


def _read_meta(jd: Path) -> dict | None:
    try:
        return json.loads((jd / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_meta(jd: Path, meta: dict) -> None:
    (jd / "meta.json").write_text(json.dumps(meta))


def _read_envelope(jd: Path) -> dict | None:
    """Parse the claude JSON envelope from result.json, or None if absent/partial."""
    try:
        text = (jd / "result.json").read_text()
    except OSError:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        return None
    return env if isinstance(env, dict) else None


@dataclass
class JobConfig:
    kind: str
    config_mode: str
    access: str
    scope: str | None
    base: str | None
    detail: str
    timeout_seconds: int
    workspace_source: str | None
    context_summary: ContextSummary | None
    requested_max_budget_usd: float | None = None
    redacted_paths: list[str] | None = None
    security_warnings: list[str] | None = None


def _write_stdin(proc: subprocess.Popen, stdin_text: str) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(stdin_text)
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.close()


def start_job(
    cmd: list[str], cwd: str, cfg: JobConfig, stdin_text: str | None = None
) -> tuple[str, str]:
    """Spawn the claude command detached and persist its record.

    Returns (job_id, started_at_iso)."""
    job_id = uuid4().hex
    jd = _job_dir(cwd, job_id)
    jd.mkdir(parents=True, exist_ok=True)
    # Best-effort: results contain the diff; keep the workspace tree user-only.
    with contextlib.suppress(OSError):
        _ws_dir(cwd).chmod(0o700)
    started = time.time()
    result_path = jd / "result.json"
    stderr_path = jd / "stderr.log"
    try:
        with result_path.open("w") as rf, stderr_path.open("w") as ef:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                stdout=rf,
                stderr=ef,
                text=True,
                encoding="utf-8",
                start_new_session=True,
            )
            if stdin_text is not None:
                threading.Thread(target=_write_stdin, args=(proc, stdin_text), daemon=True).start()
    except OSError:
        shutil.rmtree(jd, ignore_errors=True)
        raise
    summary = cfg.context_summary.model_dump() if cfg.context_summary else None
    meta = {
        "job_id": job_id,
        "kind": cfg.kind,
        "pid": proc.pid,
        "started_epoch": started,
        "started_at": datetime.now(UTC).isoformat(),
        "deadline_epoch": started + max_seconds(),
        "completed_epoch": None,
        "terminal_status": None,  # set by cancel/deadline reap
        "config": {
            "config_mode": cfg.config_mode,
            "access": cfg.access,
            "scope": cfg.scope,
            "base": cfg.base,
            "detail": cfg.detail,
            "timeout_seconds": cfg.timeout_seconds,
            "workspace_source": cfg.workspace_source,
            "cwd": cwd,
            "requested_max_budget_usd": cfg.requested_max_budget_usd,
            "redacted_paths": cfg.redacted_paths or [],
            "security_warnings": cfg.security_warnings or [],
        },
        "context_summary": summary,
    }
    _write_meta(jd, meta)
    _enforce_count_cap(cwd)
    return job_id, meta["started_at"]


def _status_of(jd: Path, meta: dict) -> str:
    """Compute the live status, killing + marking jobs that overran their deadline."""
    terminal = meta.get("terminal_status")
    if terminal:
        return terminal
    if _is_running(meta.get("pid")):
        if time.time() > meta.get("deadline_epoch", float("inf")):
            _kill_pid_tree(meta.get("pid"))
            meta["terminal_status"] = "timeout"
            meta["completed_epoch"] = time.time()
            _write_meta(jd, meta)
            return "timeout"
        return "running"
    # Process gone: done if it left a parseable envelope, else it crashed.
    if meta.get("completed_epoch") is None:
        meta["completed_epoch"] = time.time()
        _write_meta(jd, meta)
    return "done" if _read_envelope(jd) is not None else "failed"


def _elapsed_ms(meta: dict) -> int:
    end = meta.get("completed_epoch") or time.time()
    return max(0, int((end - meta.get("started_epoch", end)) * 1000))


def _deadline_seconds(meta: dict) -> int:
    """The wall-clock window the job was STARTED with (deadline minus start), not
    the current env value — so status stays consistent if the env later changes."""
    started = meta.get("started_epoch")
    deadline = meta.get("deadline_epoch")
    if started is not None and deadline is not None:
        return max(0, round(deadline - started))
    return max_seconds()


def _expires_at(meta: dict) -> str | None:
    completed = meta.get("completed_epoch")
    if completed is None:
        return None
    return datetime.fromtimestamp(completed + ttl_seconds(), UTC).isoformat()


def _reap_workspace(cwd: str) -> None:
    """Lazy maintenance: refresh statuses and delete expired terminal records."""
    ws = _ws_dir(cwd)
    if not ws.is_dir():
        return
    ttl = ttl_seconds()
    now = time.time()
    for jd in ws.iterdir():
        if not jd.is_dir():
            continue
        meta = _read_meta(jd)
        if meta is None:
            continue
        status = _status_of(jd, meta)
        if status in _TERMINAL:
            end = meta.get("completed_epoch") or meta.get("started_epoch") or now
            if now - end > ttl:
                _rmtree(jd)


def _expired(meta: dict) -> bool:
    completed = meta.get("completed_epoch")
    if completed is None:
        return False
    return time.time() - completed > ttl_seconds()


def _read_live_job(cwd: str, job_id: str) -> tuple[Path, dict, str] | None:
    """Read and refresh a single job record.

    Status/result/cancel are commonly called in tight polling loops. Refreshing
    only the requested record avoids unrelated jobs causing latency or waitpid
    races while still preserving the TTL contract for that record.
    """
    jd = _job_dir(cwd, job_id)
    meta = _read_meta(jd)
    if meta is None:
        return None
    state = _status_of(jd, meta)
    if state in _TERMINAL and _expired(meta):
        _rmtree(jd)
        return None
    return jd, meta, state


def _enforce_count_cap(cwd: str) -> None:
    ws = _ws_dir(cwd)
    cap = _int_env(MAX_COUNT_ENV, DEFAULT_MAX_COUNT)
    dirs = [d for d in ws.iterdir() if d.is_dir()] if ws.is_dir() else []
    if len(dirs) <= cap:
        return
    # Evict oldest terminal jobs first; never kill a still-running one to fit.
    scored = []
    for jd in dirs:
        meta = _read_meta(jd) or {}
        status = _status_of(jd, meta)
        scored.append((status in _TERMINAL, meta.get("started_epoch", 0.0), jd))
    scored.sort(key=lambda t: (not t[0], t[1]))  # terminal first, then oldest
    for is_terminal, _epoch, jd in scored[: max(0, len(dirs) - cap)]:
        if is_terminal:
            _rmtree(jd)


def _rmtree(jd: Path) -> None:
    try:
        for child in jd.iterdir():
            child.unlink(missing_ok=True)
        jd.rmdir()
    except OSError:
        pass


def _build_meta(meta: dict) -> Meta:
    c = meta.get("config", {})
    cwd = c.get("cwd", "")
    source = c.get("workspace_source")
    return Meta(
        cwd=cwd,
        workspace_source=source,
        workspace_warning=workspace_warning_for(source, cwd),
        config_mode=c.get("config_mode", "inherit"),
        access=c.get("access", "toolless"),
        scope=c.get("scope"),
        base=c.get("base"),
        timeout_seconds=c.get("timeout_seconds", max_seconds()),
        requested_max_budget_usd=c.get("requested_max_budget_usd"),
        redacted_paths=c.get("redacted_paths") or [],
        security_warnings=c.get("security_warnings") or [],
        elapsed_ms=_elapsed_ms(meta),
        job_id=meta.get("job_id"),
    )


def _terminal_cost(jd: Path, state: str) -> float | None:
    """Spend recorded by a terminal job, or None.

    A cancelled/timeout job can still leave a parseable (possibly partial) envelope
    that recorded cost, so we surface cost for ANY terminal state — matching the
    result path (_job_error) and the JobStatus.cost_usd contract ('terminal jobs
    that spent'), not just done."""
    if state not in _TERMINAL:
        return None
    env = _read_envelope(jd) or {}
    c = env.get("total_cost_usd")
    return float(c) if isinstance(c, (int, float)) else None


def status(cwd: str, job_id: str) -> dict | None:
    """Return a JobStatus dict, or None if the job does not exist."""
    with _JOBS_LOCK:
        live = _read_live_job(cwd, job_id)
        if live is None:
            return None
        jd, meta, state = live
        return _status_dict(jd, meta, state)


def _status_dict(jd: Path, meta: dict, state: str) -> dict:
    cost = _terminal_cost(jd, state)
    detail = None
    if state == "failed":
        detail = _stderr_tail(jd)
    return {
        "ok": True,
        "job_id": meta.get("job_id", jd.name),
        "kind": meta.get("kind", ""),
        "status": state,
        "started_at": meta.get("started_at", ""),
        "elapsed_ms": _elapsed_ms(meta),
        "deadline_seconds": _deadline_seconds(meta),
        "poll_after_ms": poll_after_ms(),
        "ttl_seconds": ttl_seconds(),
        "expires_at": _expires_at(meta),
        "result_available": state == "done",
        "cost_usd": cost,
        "detail": detail,
        "fingerprint": FINGERPRINT,
    }


def list_jobs(cwd: str) -> dict:
    """Return a JobListResult dict of the workspace's known jobs, newest first.

    Reaps first (like the other lifecycle calls), so listing can refresh statuses
    and delete expired records — it is not strictly read-only."""
    with _JOBS_LOCK:
        _reap_workspace(cwd)
        ws = _ws_dir(cwd)
        summaries = []
        if ws.is_dir():
            for jd in ws.iterdir():
                if not jd.is_dir():
                    continue
                meta = _read_meta(jd)
                if meta is None:
                    continue
                state = _status_of(jd, meta)
                summaries.append(
                    {
                        "_epoch": meta.get("started_epoch", 0.0),
                        "job_id": meta.get("job_id", jd.name),
                        "kind": meta.get("kind", ""),
                        "status": state,
                        "started_at": meta.get("started_at", ""),
                        "elapsed_ms": _elapsed_ms(meta),
                        "result_available": state == "done",
                        "expires_at": _expires_at(meta),
                        "cost_usd": _terminal_cost(jd, state),
                    }
                )
        summaries.sort(key=lambda s: s["_epoch"], reverse=True)  # newest first
        for s in summaries:
            s.pop("_epoch", None)
        return {"ok": True, "jobs": summaries, "fingerprint": FINGERPRINT}


def _stderr_tail(jd: Path, limit: int = 200) -> str | None:
    try:
        text = (jd / "stderr.log").read_text().strip()
    except OSError:
        return None
    return text[-limit:] or None


def result(cwd: str, job_id: str, consume: bool = False):
    """Return (payload, found). payload is the normalized SuccessResult|ErrorResult
    dict; found is False when no such job exists."""
    with _JOBS_LOCK:
        live = _read_live_job(cwd, job_id)
        if live is None:
            return None, False
        jd, meta, state = live
        if state == "done":
            env_text = (jd / "result.json").read_text()
            summary = meta.get("context_summary")
            ctx_summary = ContextSummary(**summary) if summary else None
            payload = normalize_envelope(
                meta.get("kind", "claude_review_changes"),
                env_text,
                _build_meta(meta),
                detail=meta.get("config", {}).get("detail", "summary"),
                context_summary=ctx_summary,
            )
            if consume:
                _rmtree(jd)
            return payload, True
        # Non-done states map to an error envelope so the contract stays ok-discriminated.
        payload = _job_error(meta, state, jd)
        return payload, True


_STATE_TO_ERROR = {
    "running": (
        "job_running",
        "The job is still running.",
        "Poll claude_job_status; call claude_job_result once status=done.",
    ),
    "cancelled": (
        "job_cancelled",
        "The job was cancelled.",
        "Start a new job; a cancelled run cannot be resumed.",
    ),
    "timeout": (
        "job_timeout",
        "The job exceeded its wall-clock deadline and was stopped.",
        "Narrow the scope or raise CC_PLUGIN_CODEX_JOB_MAX_SECONDS, then start a new job.",
    ),
}


def _job_error(meta: dict, state: str, jd: Path) -> dict:
    if state == "failed":
        tail = _stderr_tail(jd)
        # A failed job whose stderr carries a drift signature is the async twin of
        # the sync cli_contract_changed path — classify it the same way so async
        # callers get the same actionable error instead of a generic job_failed.
        if is_contract_drift(tail):
            info = contract_changed_error()
            code, message, repair, retryable = (
                info.code,
                info.message,
                info.repair,
                info.retryable,
            )
        else:
            code, message, repair = (
                "job_failed",
                f"The job failed without producing a result. {tail or ''}".strip(),
                "Run claude_status to check the CLI is installed and authenticated, then retry.",
            )
            retryable = True
    else:
        code, message, repair = _STATE_TO_ERROR.get(
            state, ("job_failed", "The job did not complete.", "Start a new job.")
        )
        retryable = state == "running"
    bmeta = _build_meta(meta)
    # Surface any spend the (possibly partial) envelope recorded.
    env = _read_envelope(jd)
    if env:
        apply_cost_usage(bmeta, env)
    return ErrorResult(
        error=ErrorInfo(
            code=cast("ErrorCode", code), message=message, repair=repair, retryable=retryable
        ),
        meta=bmeta,
    ).model_dump(mode="json", exclude_none=True)


def cancel(cwd: str, job_id: str) -> dict | None:
    """Kill a running job and mark it cancelled. Returns a JobStatus dict or None."""
    with _JOBS_LOCK:
        live = _read_live_job(cwd, job_id)
        if live is None:
            return None
        jd, meta, state = live
        if state not in _TERMINAL:
            _kill_pid_tree(meta.get("pid"))
            meta["terminal_status"] = "cancelled"
            meta["completed_epoch"] = time.time()
            _write_meta(jd, meta)
            state = "cancelled"
        return _status_dict(jd, meta, state)
