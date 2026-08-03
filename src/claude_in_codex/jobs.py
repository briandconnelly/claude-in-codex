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
import importlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from claude_in_codex.claude import contract_changed_error
from claude_in_codex.cli_contract import is_contract_drift
from claude_in_codex.context import redact_text
from claude_in_codex.normalize import apply_cost_usage, normalize_envelope
from claude_in_codex.schemas import (
    FINGERPRINT,
    JOB_ID_PATTERN,
    ContextSummary,
    ErrorCode,
    ErrorInfo,
    ErrorResult,
    Meta,
    branch_range,
    workspace_warning_for,
)

try:
    fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

STATE_ENV = "CLAUDE_IN_CODEX_STATE_DIR"
TTL_ENV = "CLAUDE_IN_CODEX_JOB_TTL"
MAX_SECONDS_ENV = "CLAUDE_IN_CODEX_JOB_MAX_SECONDS"
MAX_COUNT_ENV = "CLAUDE_IN_CODEX_JOB_MAX_COUNT"

DEFAULT_TTL = 86_400  # delete terminal job records after 24h
DEFAULT_MAX_SECONDS = 1_800  # wall-clock cap; a poll past this reaps the job
DEFAULT_MAX_COUNT = 50  # retained jobs per workspace; evict oldest terminal

_TERMINAL = {"done", "failed", "cancelled", "timeout"}
_JOBS_LOCK = threading.RLock()
_PROCESS_OWNER = uuid4().hex
_OWNED_PIDS: set[int] = set()
_JOB_ID_RE = re.compile(JOB_ID_PATTERN)
_TERMINATE_GRACE_SECONDS = 5.0
_LEGACY_STDERR_WITHHELD = "legacy job diagnostics withheld because they predate sanitization"


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
    return Path.home() / ".cache" / "claude-in-codex" / "jobs"


def _ws_dir(cwd: str) -> Path:
    canonical = os.path.realpath(cwd)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    # os.path.basename on the realpath string keeps the dir-name derivation stable
    # (and matches the digest input); Path.name differs on trailing-slash handling.
    base = os.path.basename(canonical.rstrip("/")) or "workspace"  # noqa: PTH119
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in base)[:40] or "ws"
    return _state_root() / f"{safe}-{digest}"


def _valid_job_id(job_id: object) -> bool:
    return isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id) is not None


def _job_dir(cwd: str, job_id: str) -> Path:
    """Return a confined direct child of the workspace job-state directory."""
    if not _valid_job_id(job_id):
        raise ValueError("job_id must be exactly 32 lowercase hexadecimal characters")
    ws = _ws_dir(cwd)
    target = ws / job_id
    if target.is_symlink() or target.resolve(strict=False).parent != ws.resolve(strict=False):
        raise ValueError("job_id does not resolve to a confined job directory")
    return target


def _job_dirs(ws: Path) -> list[Path]:
    """Enumerate only canonical, non-symlink job directories."""
    if not ws.is_dir():
        return []
    resolved_ws = ws.resolve(strict=False)
    result: list[Path] = []
    for entry in ws.iterdir():
        if not _valid_job_id(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        if entry.resolve(strict=False).parent == resolved_ws:
            result.append(entry)
    return result


def _reservation_path(cwd: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return _ws_dir(cwd) / f"idem-{digest}.json"


def reserve_idempotency_key(cwd: str, key: str, job_id: str) -> str | None:
    """Atomically reserve (workspace, key) for job_id.

    Returns None if we won the reservation, else the job_id that holds it.
    The marker is published via write-to-temp-then-os.link: the payload is
    fully written to a private temp file first, then published with
    os.link(tmp, path), which atomically fails with FileExistsError if the
    marker already exists — on a local filesystem this is atomic across
    processes, and unlike a bare O_CREAT|O_EXCL open it never exposes a
    partially-written marker to a racing reader (link() only publishes a fully
    written inode). A stale marker (its job record is gone AND the marker is
    older than the job TTL) is replaced. Written before the job spawns; the
    caller removes it via release_idempotency_key on spawn failure.

    Replacing a judged-stale marker (unlink then link) has a narrow TOCTOU
    window between two concurrent replacers; a stale marker only exists after
    a crash or TTL expiry, so this is accepted rather than closed with
    quarantine-rename."""
    if not _valid_job_id(job_id):
        raise ValueError("job_id must be exactly 32 lowercase hexadecimal characters")
    path = _reservation_path(cwd, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"job_id": job_id, "created_epoch": time.time()})
    tag = f"{os.getpid()}.{threading.get_ident()}.{uuid4().hex}"
    tmp_path = path.with_name(f".{path.name}.{tag}.tmp")
    while True:
        tmp_path.write_text(payload)
        try:
            try:
                os.link(tmp_path, path)
            except FileExistsError:
                pass  # fall through to the staleness check below
            except OSError:
                # Filesystem without hardlink support (e.g. some SMB/FUSE
                # mounts): degrade to a best-effort replace instead of failing
                # the keyed launch outright. Not atomic-exclusive, but matches
                # the previous best-effort (pre-hardlink) behavior.
                tmp_path.replace(path)
                return None
            else:
                return None
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        try:
            holder = json.loads(path.read_text())
        except FileNotFoundError:
            # The marker vanished between our link-failure and this read —
            # another caller released or replaced it in that gap. Retry
            # without unlinking; blindly unlinking here could delete a marker
            # published concurrently by that other caller.
            continue
        except (OSError, json.JSONDecodeError):
            holder = None
        if isinstance(holder, dict) and _valid_job_id(holder.get("job_id")):
            held_id = str(holder["job_id"])
            try:
                record_alive = _read_meta(_job_dir(cwd, held_id)) is not None
            except ValueError:
                record_alive = False
            created = holder.get("created_epoch") or 0
            if record_alive or (time.time() - created) <= ttl_seconds():
                return held_id
        # Stale or unreadable marker: remove and retry the exclusive create.
        with contextlib.suppress(OSError):
            path.unlink()
        continue


def release_idempotency_key(cwd: str, key: str, job_id: str) -> None:
    """Drop our reservation (spawn failed). Only removes our own marker."""
    if not _valid_job_id(job_id):
        return
    path = _reservation_path(cwd, key)
    try:
        holder = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(holder, dict) and holder.get("job_id") == job_id:
        with contextlib.suppress(OSError):
            path.unlink()


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_running(pid: int | None) -> bool:
    """Whether a process launched by this server is still running.

    The job is launched detached but is still our child until it exits, so we
    must reap it with waitpid — otherwise it lingers as a zombie that kill(0)
    reports as 'alive' forever. waitpid(WNOHANG) returns (pid, _) once it exits
    (reaping it) and (0, 0) while it runs. A PID tracked as ours that is no
    longer waitable is discarded rather than trusted after possible reuse.
    Untracked callers retain the historical kill(0) liveness probe; job
    lifecycle code never uses that alone as an ownership proof."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            _OWNED_PIDS.discard(pid)
            return False
        if reaped == 0:
            return True
    except ChildProcessError:
        if pid in _OWNED_PIDS:
            # A PID launched by this process that is no longer waitable has
            # already been reaped. Never let a reused PID regain ownership.
            _OWNED_PIDS.discard(pid)
            return False
    except OSError:
        _OWNED_PIDS.discard(pid)
        return False
    return _pid_alive(pid)


def _worker_lock_held(jd: Path) -> bool | None:
    """Whether another process positively holds this job's worker lock.

    ``None`` means ownership cannot be verified (missing/corrupt lock or a
    platform without advisory flock support), which callers treat as not owned.
    """
    path = jd / "worker.lock"
    if path.is_symlink() or not path.is_file():
        return None
    if fcntl is None:  # pragma: no cover - non-POSIX fallback
        return None
    try:
        lock_file = path.open("r+b")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return False
    finally:
        lock_file.close()


def _job_running(jd: Path, meta: dict) -> bool:
    pid = meta.get("pid")
    lock_held = _worker_lock_held(jd)
    if lock_held is True:
        return _pid_alive(pid)
    # The parent may publish metadata before the new worker has acquired its
    # lock. Trust only PIDs that this exact server process has not yet reaped.
    if meta.get("owner") == _PROCESS_OWNER and pid in _OWNED_PIDS:
        return _is_running(pid)
    return False


def _signal_job(pid: int, sig: signal.Signals) -> None:
    """Signal only the verified worker session or, conservatively, its PID."""
    try:
        if hasattr(os, "killpg") and os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _terminate_verified(jd: Path, meta: dict) -> None:
    """Terminate a job only while its worker ownership remains provable."""
    pid = meta.get("pid")
    if not pid:
        return
    if not _job_running(jd, meta):
        return
    _signal_job(pid, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _job_running(jd, meta):
            return
        time.sleep(0.02)
    if _job_running(jd, meta):
        _signal_job(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError, OSError):
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                _OWNED_PIDS.discard(pid)


def _read_meta(jd: Path) -> dict | None:
    try:
        meta = json.loads((jd / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    if _valid_job_id(jd.name) and meta.get("job_id") != jd.name:
        return None
    return meta


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
    head: str | None
    detail: str
    timeout_seconds: int
    workspace_source: str | None
    context_summary: ContextSummary | None
    requested_max_budget_usd: float | None = None
    configured_max_budget_usd: float | None = None
    effective_max_budget_usd: float | None = None
    paths: list[str] | None = None
    redacted_paths: list[str] | None = None
    security_warnings: list[str] | None = None
    idempotency_key: str | None = None


def _write_stdin(proc: subprocess.Popen, stdin_text: str) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(stdin_text)
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.close()


def _check_executable(cmd: list[str], cwd: str) -> None:
    """Preserve Popen's immediate missing/non-executable command failure."""
    if not cmd:
        raise FileNotFoundError("job command is empty")
    executable = cmd[0]
    if Path(executable).name != executable:
        path = Path(executable)
        if not path.is_absolute():
            path = Path(cwd) / path
        if not path.is_file():
            raise FileNotFoundError(executable)
        if not os.access(path, os.X_OK):
            raise PermissionError(executable)
    elif shutil.which(executable) is None:
        raise FileNotFoundError(executable)


def start_job(
    cmd: list[str],
    cwd: str,
    cfg: JobConfig,
    stdin_text: str | None = None,
    *,
    job_id: str | None = None,
) -> tuple[str, str]:
    """Spawn the claude command detached and persist its record.

    job_id lets a caller pre-reserve the id (e.g. via reserve_idempotency_key)
    before spawning; when omitted, one is generated here as before.

    Returns (job_id, started_at_iso)."""
    job_id = job_id or uuid4().hex
    _check_executable(cmd, cwd)
    jd = _job_dir(cwd, job_id)
    jd.mkdir(parents=True, exist_ok=False)
    # Best-effort: results contain the diff; keep the workspace tree user-only.
    with contextlib.suppress(OSError):
        _ws_dir(cwd).chmod(0o700)
    started = time.time()
    result_path = jd / "result.json"
    stderr_path = jd / "stderr.log"
    lock_path = jd / "worker.lock"
    worker_cmd = [
        sys.executable,
        "-m",
        "claude_in_codex._job_worker",
        "--lock-path",
        str(lock_path),
        "--stderr-path",
        str(stderr_path),
        "--",
        *cmd,
    ]
    try:
        with result_path.open("w") as rf:
            proc = subprocess.Popen(
                worker_cmd,
                cwd=cwd,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                stdout=rf,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                start_new_session=True,
            )
            _OWNED_PIDS.add(proc.pid)
            if stdin_text is not None:
                threading.Thread(target=_write_stdin, args=(proc, stdin_text), daemon=True).start()
    except OSError:
        shutil.rmtree(jd, ignore_errors=True)
        raise
    summary = cfg.context_summary.model_dump() if cfg.context_summary else None
    meta = {
        "job_id": job_id,
        "kind": cfg.kind,
        "idempotency_key": cfg.idempotency_key,
        "pid": proc.pid,
        "owner": _PROCESS_OWNER,
        "stderr_sanitized": True,
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
            "head": cfg.head,
            "detail": cfg.detail,
            "timeout_seconds": cfg.timeout_seconds,
            "workspace_source": cfg.workspace_source,
            "cwd": cwd,
            "requested_max_budget_usd": cfg.requested_max_budget_usd,
            "configured_max_budget_usd": cfg.configured_max_budget_usd,
            "effective_max_budget_usd": cfg.effective_max_budget_usd,
            "paths": cfg.paths,
            "redacted_paths": cfg.redacted_paths or [],
            "security_warnings": cfg.security_warnings or [],
        },
        "context_summary": summary,
    }
    _write_meta(jd, meta)
    _enforce_count_cap(cwd)
    return job_id, meta["started_at"]


def find_by_idempotency_key(cwd: str, key: str) -> str | None:
    """Newest non-expired job started with this idempotency key, or None.

    Dedup window and scope: per workspace, for the lifetime of the job record
    (its TTL) — the same window in which claude_job_list can see the job."""
    with _JOBS_LOCK:
        ws = _ws_dir(cwd)
        if not ws.is_dir():
            return None
        matches: list[tuple[float, str]] = []
        for jd in _job_dirs(ws):
            meta = _read_meta(jd)
            if meta is None or meta.get("idempotency_key") != key:
                continue
            state = _status_of(jd, meta)
            if state in _TERMINAL and _expired(meta):
                continue
            matches.append((meta.get("started_epoch", 0.0), meta.get("job_id", jd.name)))
        if not matches:
            return None
        return max(matches)[1]


def _status_of(jd: Path, meta: dict) -> str:
    """Compute the live status, killing + marking jobs that overran their deadline."""
    terminal = meta.get("terminal_status")
    if terminal:
        return terminal
    # A complete envelope wins races with worker exit, cancellation, and deadline
    # enforcement. It is safe to normalize as soon as the one-shot JSON is whole.
    if _read_envelope(jd) is not None:
        if meta.get("completed_epoch") is None:
            meta["completed_epoch"] = time.time()
            _write_meta(jd, meta)
        return "done"
    if _job_running(jd, meta):
        if time.time() > meta.get("deadline_epoch", float("inf")):
            _terminate_verified(jd, meta)
            if _read_envelope(jd) is not None:
                meta["completed_epoch"] = meta.get("completed_epoch") or time.time()
                _write_meta(jd, meta)
                return "done"
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
            if jd.name.startswith("idem-") and jd.name.endswith(".json"):
                _reap_stale_marker(cwd, jd, now, ttl)
            continue
        if jd.is_symlink() or not _valid_job_id(jd.name):
            continue
        meta = _read_meta(jd)
        if meta is None:
            continue
        status = _status_of(jd, meta)
        if status in _TERMINAL:
            end = meta.get("completed_epoch") or meta.get("started_epoch") or now
            if now - end > ttl:
                _rmtree(jd)


def _reap_stale_marker(cwd: str, marker: Path, now: float, ttl: int) -> None:
    """Delete an idempotency marker whose reserved job record no longer exists
    AND whose creation predates the TTL — same staleness rule used by
    reserve_idempotency_key."""
    try:
        holder = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(holder, dict):
        return
    held_id = holder.get("job_id")
    if not _valid_job_id(held_id):
        with contextlib.suppress(OSError):
            marker.unlink()
        return
    created = holder.get("created_epoch") or 0
    if now - created <= ttl:
        return
    try:
        if _read_meta(_job_dir(cwd, str(held_id))) is not None:
            return
    except ValueError:
        pass
    with contextlib.suppress(OSError):
        marker.unlink()


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
    if not _valid_job_id(job_id):
        raise ValueError("job_id must be exactly 32 lowercase hexadecimal characters")
    try:
        jd = _job_dir(cwd, job_id)
    except ValueError:
        # A valid-looking name that resolves through a symlink is corrupt state,
        # not a job record the lifecycle API may follow.
        return None
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
    dirs = _job_dirs(ws)
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
    scope = c.get("scope")
    # Recompute diff_range from the stored base+head inputs so it cannot drift from
    # what the job was started with; head defaults to HEAD for branch scope.
    head, diff_range = branch_range(scope, c.get("base"), c.get("head"))
    return Meta(
        cwd=cwd,
        workspace_source=source,
        workspace_warning=workspace_warning_for(source, cwd),
        config_mode=c.get("config_mode", "inherit"),
        access=c.get("access", "toolless"),
        scope=scope,
        base=c.get("base"),
        head=head,
        diff_range=diff_range,
        paths=c.get("paths"),
        timeout_seconds=c.get("timeout_seconds", max_seconds()),
        requested_max_budget_usd=c.get("requested_max_budget_usd"),
        configured_max_budget_usd=c.get("configured_max_budget_usd"),
        effective_max_budget_usd=c.get(
            "effective_max_budget_usd", c.get("requested_max_budget_usd")
        ),
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
        detail = _stderr_tail(jd, meta)
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
            for jd in _job_dirs(ws):
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


def _stderr_tail(jd: Path, meta: dict, limit: int = 200) -> str | None:
    if meta.get("stderr_sanitized") is not True:
        return _LEGACY_STDERR_WITHHELD
    try:
        text = (jd / "stderr.log").read_text().strip()
    except OSError:
        return None
    # Defense in depth for records written by an interrupted or older worker.
    return redact_text(text)[0][-limit:] or None


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
        "Narrow the scope or raise CLAUDE_IN_CODEX_JOB_MAX_SECONDS, then start a new job.",
    ),
}


def _job_error(meta: dict, state: str, jd: Path) -> dict:
    if state == "failed":
        tail = _stderr_tail(jd, meta)
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
    repair_tool = None
    repair_arguments = None
    if code == "job_running":
        repair_tool = "claude_job_status"
        repair_arguments = {"job_id": meta.get("job_id")}
    return ErrorResult(
        error=ErrorInfo(
            code=cast("ErrorCode", code),
            message=message,
            repair=repair,
            retryable=retryable,
            repair_tool=repair_tool,
            repair_arguments=repair_arguments,
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
            _terminate_verified(jd, meta)
            if _read_envelope(jd) is not None:
                meta["completed_epoch"] = meta.get("completed_epoch") or time.time()
                _write_meta(jd, meta)
                state = "done"
            else:
                meta["terminal_status"] = "cancelled"
                meta["completed_epoch"] = time.time()
                _write_meta(jd, meta)
                state = "cancelled"
        return _status_dict(jd, meta, state)
