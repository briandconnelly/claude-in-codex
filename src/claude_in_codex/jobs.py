"""Job layer: wire mapping and result synthesis over the pontonier job store.

Lifecycle MECHANICS — detached spawn, worker-lock liveness, deadline reaping,
TTL cleanup, count caps, cancellation, idempotent starts — live in
``pontonier.core.jobs.JobStore``. This module is the consumer layer that
remains deliberately local: the wire shapes (JobStatus/JobListResult dicts),
result synthesis (re-rendering the stored claude envelope at fetch-time
detail, drift upgrading, cost surfacing, repair actions), stderr-tail
selection, and compatibility with records written by 0.7.x.

Layout compatibility: the 0.7 store and the pontonier store share the same
state root, workspace-dir naming, and meta field names (same lineage), so
legacy records are read, cancelled, and TTL-reaped by the same store. The
differences are per record: legacy metas carry ``config``/``context_summary``
at top level (new records carry them under ``extra``), legacy sanitized child
stderr lives in ``stderr.log`` (new records use ``claude-stderr.log`` because
the store's own ``stderr.log`` now captures worker self-diagnostics), and
legacy keyed launches are replayed through their on-disk ``idem-*.json``
markers, which are read and reaped — but no longer written; new keyed
launches go through the store's idempotency index.

The prompt is streamed to the worker over a pipe (`stdin_text`) and the
worker's child inherits that pipe — the prompt never lands on disk or argv,
same as 0.7.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from pontonier.core.jobs import DiscardOutcome, JobStore

from claude_in_codex.claude import contract_changed_error
from claude_in_codex.cli_contract import is_contract_drift
from claude_in_codex.context import sanitize_echo_prose
from claude_in_codex.normalize import apply_cost_usage, normalize_envelope
from claude_in_codex.schemas import (
    FINGERPRINT,
    JOB_ID_PATTERN,
    ContextSummary,
    ErrorCode,
    ErrorDetails,
    ErrorInfo,
    ErrorResult,
    Meta,
    RepairAction,
    branch_range,
    workspace_warning_for,
)

STATE_ENV = "CLAUDE_IN_CODEX_STATE_DIR"
TTL_ENV = "CLAUDE_IN_CODEX_JOB_TTL"
MAX_SECONDS_ENV = "CLAUDE_IN_CODEX_JOB_MAX_SECONDS"
MAX_COUNT_ENV = "CLAUDE_IN_CODEX_JOB_MAX_COUNT"

DEFAULT_TTL = 86_400  # delete terminal job records after 24h
DEFAULT_MAX_SECONDS = 1_800  # wall-clock cap; a poll past this reaps the job
DEFAULT_MAX_COUNT = 50  # retained jobs per workspace; evict oldest terminal

_TERMINAL = {"done", "failed", "cancelled", "timeout"}
_JOBS_LOCK = threading.RLock()
_JOB_ID_RE = re.compile(JOB_ID_PATTERN)
_LEGACY_STDERR_WITHHELD = "legacy job diagnostics withheld because they predate sanitization"
# New records' sanitized child stderr. The store owns <job_dir>/stderr.log for
# the worker's OWN diagnostics, so the redacted claude stream needs its own file.
_CLAUDE_STDERR_FILE = "claude-stderr.log"
# The store's idempotency index keys on (tool, key); one tool starts keyed jobs.
_IDEMPOTENT_TOOL = "claude_review_changes_async"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def max_seconds() -> int:
    return _int_env(MAX_SECONDS_ENV, DEFAULT_MAX_SECONDS)


def ttl_seconds() -> int:
    return _int_env(TTL_ENV, DEFAULT_TTL)


def max_count() -> int:
    return _int_env(MAX_COUNT_ENV, DEFAULT_MAX_COUNT)


def poll_after_ms() -> int:
    return 1000


def _state_root() -> Path:
    root = os.environ.get(STATE_ENV)
    if root:
        return Path(root)
    return Path.home() / ".cache" / "claude-in-codex" / "jobs"


def _store() -> JobStore:
    """A store view over the current env knobs. Cheap to build per call, which
    preserves 0.7's read-env-at-call-time behavior for the TTL/deadline/cap."""
    return JobStore(
        root=_state_root(),
        ttl_seconds=ttl_seconds(),
        max_seconds=max_seconds(),
        max_count=max_count(),
        poll_after_ms=poll_after_ms(),
    )


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


def _read_envelope(jd: Path, *, include_pending: bool = False) -> dict | None:
    """Parse the claude JSON envelope from result.json, or None if absent/partial.

    With ``include_pending``, fall back to the worker's unrenamed
    ``result.json.tmp``. The worker publishes atomically (tmp + rename after the
    child exits), so a worker killed in the terminate grace window leaves a
    COMPLETE envelope under the temporary name and the record reports no spend
    for a run that was already paid for. The JSON parse below is the safety
    gate: a torn write cannot parse as an object, so a partial envelope is never
    surfaced. Callers pass include_pending only for terminal states, where the
    store has already decided the outcome and cannot be misled by what we read.
    """
    found = _read_envelope_text(jd, include_pending=include_pending)
    return None if found is None else found[0]


def _read_envelope_text(jd: Path, *, include_pending: bool = False) -> tuple[dict, str] | None:
    """As ``_read_envelope``, but also return the TEXT that parsed.

    ``result()`` re-renders the stored envelope rather than a cached payload, so
    it needs the source text of whichever name actually parsed — which is not
    always ``result.json`` once a terminal record can be promoted off its
    unpublished ``result.json.tmp``."""
    for name in ("result.json", "result.json.tmp") if include_pending else ("result.json",):
        try:
            text = (jd / name).read_text().strip()
        except OSError:
            continue
        if not text:
            continue
        try:
            env = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(env, dict):
            return env, text
    return None


# --------------------------------------------------------------- record shapes
# Legacy (0.7) records carry config/context_summary/idempotency_key at the meta
# top level; pontonier-store records carry them under meta["extra"]. These
# accessors are the single place that knows both shapes.


def _record_extra(meta: dict) -> dict:
    extra = meta.get("extra")
    return extra if isinstance(extra, dict) else {}


def _record_config(meta: dict) -> dict:
    c = _record_extra(meta).get("config")
    if isinstance(c, dict):
        return c
    c = meta.get("config")
    return c if isinstance(c, dict) else {}


def _record_context_summary(meta: dict) -> dict | None:
    s = _record_extra(meta).get("context_summary")
    if isinstance(s, dict):
        return s
    s = meta.get("context_summary")
    return s if isinstance(s, dict) else None


def _record_stderr_file(meta: dict) -> str:
    name = _record_extra(meta).get("stderr_file")
    return name if isinstance(name, str) and name else "stderr.log"


def _record_stderr_sanitized(meta: dict) -> bool:
    if _record_extra(meta).get("stderr_sanitized") is True:
        return True
    return meta.get("stderr_sanitized") is True


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


def _extra_for(cfg: JobConfig, cwd: str) -> dict:
    summary = cfg.context_summary.model_dump() if cfg.context_summary else None
    return {
        "idempotency_key": cfg.idempotency_key,
        "stderr_file": _CLAUDE_STDERR_FILE,
        "stderr_sanitized": True,
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


def _worker_factory(cmd: list[str], workspace: str, *, config_mode: str):
    def factory(jd: Path) -> list[str]:
        # The executable is checked HERE rather than before the store call. The
        # store invokes this factory only when it is about to spawn, so a keyed
        # replay/conflict/in_progress outcome no longer depends on `claude`
        # still being on PATH — recovery must not need a resource it never uses.
        # The store creates jd before calling us and only cleans up around its
        # own Popen, so drop the directory we were handed before re-raising.
        try:
            _check_executable(cmd, workspace)
        except OSError:
            shutil.rmtree(jd, ignore_errors=True)
            raise
        return [
            sys.executable,
            "-m",
            "claude_in_codex._job_worker",
            "--lock-path",
            str(jd / "worker.lock"),
            "--stderr-path",
            str(jd / _CLAUDE_STDERR_FILE),
            "--result-path",
            str(jd / "result.json"),
            # The store spawns the WORKER with cwd=<job_dir> so a relative
            # result.json lands in the record. The CHILD must still run in the
            # workspace, or inherit/scoped stop loading the workspace CLAUDE.md
            # and .claude/settings*.json and relative reads resolve in the cache.
            "--workspace",
            workspace,
            # The store has no environment channel, so the worker applies the
            # per-mode credential policy itself. Without this a detached
            # inherit/scoped/safe run inherits a stale ANTHROPIC_API_KEY the
            # equivalent synchronous run strips.
            "--config-mode",
            config_mode,
            "--",
            *cmd,
        ]

    return factory


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
) -> tuple[str, str]:
    """Spawn the claude command detached via the store and persist its record.

    Returns (job_id, started_at_iso)."""
    with _JOBS_LOCK:
        return _store().start(
            _worker_factory(cmd, cwd, config_mode=cfg.config_mode),
            cwd,
            kind=cfg.kind,
            extra=_extra_for(cfg, cwd),
            stdin_text=stdin_text,
        )


def arg_hash_for(cmd: list[str], prompt: str | None) -> str:
    """The effective-argument digest a keyed launch is deduplicated under.

    Two launches with the same idempotency_key but different effective arguments
    are a conflict, not a replay. The pair (argv, prompt) IS the effective
    argument set: argv carries model, effort, budget, access, and config-mode
    flags, and the prompt carries scope, base, head, paths, focus, and the
    gathered diff. Hashing them directly means a newly added tool parameter
    cannot silently fall out of the digest the way an enumerated field list
    allowed — `focus`, `model`, and `reasoning_effort` all did.

    Volatile bookkeeping (timeouts, workspace provenance, redaction counts) is
    excluded by construction: it appears in neither argv nor the prompt.
    """
    material = json.dumps({"argv": list(cmd), "prompt": prompt}, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


def start_job_idempotent(
    cmd: list[str],
    cwd: str,
    cfg: JobConfig,
    stdin_text: str | None,
    *,
    key: str,
) -> dict:
    """Deduplicated start through the store's idempotency index.

    Returns the store outcome dict: kind is one of created (with job_id and
    started_at), replay (with job_id), conflict, unavailable, in_progress, or
    io_error."""
    with _JOBS_LOCK:
        return _store().start_idempotent(
            _worker_factory(cmd, cwd, config_mode=cfg.config_mode),
            cwd,
            kind=cfg.kind,
            tool=_IDEMPOTENT_TOOL,
            key=key,
            arg_hash=arg_hash_for(cmd, stdin_text),
            extra=_extra_for(cfg, cwd),
            stdin_text=stdin_text,
        )


# ------------------------------------------------------- legacy keyed launches
# 0.7 keyed launches published idem-<sha16>.json markers in the workspace dir.
# They are still read (replay) and reaped (staleness), but never written.


def _reservation_path(cwd: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return _ws_dir(cwd) / f"idem-{digest}.json"


def find_by_idempotency_key(cwd: str, key: str) -> str | None:
    """The job_id a legacy (0.7) marker reserved for this key, or None.

    Only markers whose record still exists count — a marker for a reaped job
    must not shadow a fresh launch."""
    path = _reservation_path(cwd, key)
    try:
        holder = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(holder, dict):
        return None
    held = holder.get("job_id")
    if not _valid_job_id(held):
        return None
    try:
        if _read_meta(_job_dir(cwd, str(held))) is None:
            return None
    except ValueError:
        return None
    return str(held)


def _reap_stale_markers(cwd: str) -> None:
    """Delete legacy idempotency markers whose reserved job record no longer
    exists AND whose creation predates the TTL."""
    ws = _ws_dir(cwd)
    if not ws.is_dir():
        return
    now = time.time()
    ttl = ttl_seconds()
    for marker in ws.iterdir():
        if not (marker.name.startswith("idem-") and marker.name.endswith(".json")):
            continue
        try:
            holder = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(holder, dict):
            continue
        held_id = holder.get("job_id")
        if not _valid_job_id(held_id):
            with contextlib.suppress(OSError):
                marker.unlink()
            continue
        created = holder.get("created_epoch") or 0
        if now - created <= ttl:
            continue
        try:
            if _read_meta(_job_dir(cwd, str(held_id))) is not None:
                continue
        except ValueError:
            pass
        with contextlib.suppress(OSError):
            marker.unlink()


# ------------------------------------------------------------------ wire dicts


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


def _build_meta(meta: dict) -> Meta:
    c = _record_config(meta)
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


_PROMOTABLE = {"cancelled", "timeout", "failed"}


def _promoted_state(jd: Path, state: str) -> str:
    """A complete envelope wins races with cancellation and deadline reaping.

    The worker publishes atomically (tmp + rename after the child exits), so a
    worker killed inside the terminate grace window leaves a COMPLETE envelope
    under ``result.json.tmp`` for a run that was already PAID for. Left as
    cancelled/timeout, that job bills the caller — ``_terminal_cost`` recovers
    the spend from the very same file — while making the answer unreachable.

    This restores 0.7's ``_status_of`` behavior ("a complete envelope wins races
    with worker exit, cancellation, and deadline enforcement") for the one case
    the atomic-publish worker introduced. The JSON parse is the gate, exactly as
    it is for cost: a torn write cannot parse as an object, so a partial envelope
    is never promoted. Only terminal states are considered, so a running job's
    live ``.tmp`` is never read.

    Deliberately narrow: this promotes ONLY when the envelope exists solely
    under the unpublished name, which is precisely when the store never had a
    publishable result to decide on. A record that IS carrying a published
    ``result.json`` and is still marked cancelled was decided that way on
    purpose — the store saw the result and the cancellation won — and keeps its
    terminal status with its spend surfaced (see
    ``test_terminal_nondone_job_surfaces_cost``).
    """
    if state not in _PROMOTABLE:
        return state
    if _read_envelope(jd) is not None:  # published: the store already decided
        return state
    return "done" if _read_envelope(jd, include_pending=True) is not None else state


def _terminal_cost(jd: Path, state: str) -> float | None:
    """Spend recorded by a terminal job, or None.

    A cancelled/timeout job can still leave a parseable (possibly partial) envelope
    that recorded cost, so we surface cost for ANY terminal state — matching the
    result path (_job_error) and the JobStatus.cost_usd contract ('terminal jobs
    that spent'), not just done."""
    if state not in _TERMINAL:
        return None
    env = _read_envelope(jd, include_pending=True) or {}
    c = env.get("total_cost_usd")
    return float(c) if isinstance(c, (int, float)) else None


def _stderr_tail(jd: Path, meta: dict, limit: int = 200) -> str | None:
    if not _record_stderr_sanitized(meta):
        return _LEGACY_STDERR_WITHHELD
    try:
        text = (jd / _record_stderr_file(meta)).read_text().strip()
    except OSError:
        return None
    # Defense in depth for records written by an interrupted or older worker,
    # and the control-character strip the on-disk line redactor cannot do.
    return sanitize_echo_prose(text)[-limit:] or None


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


def _refresh(cwd: str, job_id: str) -> tuple[Path, dict, str] | None:
    """Delegate liveness/deadline/TTL refresh to the store, then reread the
    (store-updated) meta for this layer's richer wire mapping."""
    if not _valid_job_id(job_id):
        raise ValueError("job_id must be exactly 32 lowercase hexadecimal characters")
    sd = _store().status(cwd, job_id)
    if sd is None:
        return None
    try:
        jd = _job_dir(cwd, job_id)
    except ValueError:
        return None
    meta = _read_meta(jd)
    if meta is None:
        return None
    return jd, meta, _promoted_state(jd, sd["status"])


def status(cwd: str, job_id: str) -> dict | None:
    """Return a JobStatus dict, or None if the job does not exist."""
    with _JOBS_LOCK:
        live = _refresh(cwd, job_id)
        if live is None:
            return None
        jd, meta, state = live
        return _status_dict(jd, meta, state)


def list_jobs(cwd: str) -> dict:
    """Return a JobListResult dict of the workspace's known jobs, newest first.

    Reaps first (like the other lifecycle calls), so listing can refresh statuses
    and delete expired records — it is not strictly read-only."""
    with _JOBS_LOCK:
        _reap_stale_markers(cwd)
        summaries = []
        for sd in _store().list_jobs(cwd):
            job_id = sd.get("job_id", "")
            try:
                jd = _job_dir(cwd, job_id)
            except ValueError:
                continue
            meta = _read_meta(jd) or {}
            # Promote here too: claude_job_list must not disagree with
            # claude_job_status about whether a result can be fetched.
            state = _promoted_state(jd, sd["status"])
            summaries.append(
                {
                    "_epoch": meta.get("started_epoch", 0.0),
                    "job_id": job_id,
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


def cancel(cwd: str, job_id: str) -> dict | None:
    """Kill a running job and mark it cancelled. Returns a JobStatus dict or None."""
    with _JOBS_LOCK:
        if not _valid_job_id(job_id):
            raise ValueError("job_id must be exactly 32 lowercase hexadecimal characters")
        sd = _store().cancel(cwd, job_id)
        if sd is None:
            return None
        try:
            jd = _job_dir(cwd, job_id)
        except ValueError:
            return None
        meta = _read_meta(jd)
        if meta is None:
            return None
        return _status_dict(jd, meta, _promoted_state(jd, sd["status"]))


def result(cwd: str, job_id: str, consume: bool = False, detail: str | None = None):
    """Return (payload, found). payload is the normalized SuccessResult|ErrorResult
    dict; found is False when no such job exists.

    The raw envelope is stored, not the rendered result, so `detail` re-renders it
    at fetch time: passing "full" recovers content a bounded summary truncated (#94)
    without another paid call. None keeps the level the job was started with —
    EXCEPT when consuming, where None renders at full detail: deletion is
    irreversible, so the last read of a record must hand back everything the caller
    already paid for rather than silently destroying what a summary cap dropped. An
    explicit `detail` is still honored, so a caller can opt into the cheap final
    read; the truncation block on that result then points at a paid re-run, never at
    the record this call is deleting."""
    with _JOBS_LOCK:
        live = _refresh(cwd, job_id)
        if live is None:
            return None, False
        jd, meta, state = live
        if state == "done":
            # Not necessarily result.json: a promoted terminal record renders
            # from the unpublished result.json.tmp the worker never got to
            # rename. _promoted_state has already proved this parses.
            found = _read_envelope_text(jd, include_pending=True)
            if found is None:
                return _job_error(meta, "failed", jd), True
            env_text = found[1]
            summary = _record_context_summary(meta)
            ctx_summary = ContextSummary(**summary) if summary else None
            configured = _record_config(meta).get("detail", "summary")
            payload = normalize_envelope(
                meta.get("kind", "claude_review_changes"),
                env_text,
                _build_meta(meta),
                detail=detail or ("full" if consume else configured),
                context_summary=ctx_summary,
                record_survives=not consume,
            )
            if consume:
                outcome = _store().discard(cwd, job_id)
                # DELETE_FAILED leaves a fully readable record for the TTL
                # reaper — same degradation as 0.7's best-effort removal.
                _ = outcome is DiscardOutcome.REMOVED
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
                "Run claude_status to check the CLI is installed and authenticated, "
                "then start a new job.",
            )
            # NOT retryable: the record is terminal, so re-fetching this job_id
            # returns job_failed forever. The recoverable action is to diagnose
            # and launch again, which is a different call.
            retryable = False
    else:
        code, message, repair = _STATE_TO_ERROR.get(
            state, ("job_failed", "The job did not complete.", "Start a new job.")
        )
        retryable = state == "running"
    bmeta = _build_meta(meta)
    # Surface any spend the (possibly partial) envelope recorded. Match
    # _terminal_cost's discipline: include_pending only for a terminal state,
    # where the store has already decided the outcome and the .tmp cannot be a
    # live stream still being written (jobs.py review, PR #106) — a running
    # job's .tmp is live and must not be read here.
    env = _read_envelope(jd, include_pending=state in _TERMINAL)
    if env:
        apply_cost_usage(bmeta, env)
    action = None
    retry_after_ms = None
    if code == "job_running":
        # Poll status rather than re-fetching the result: status is the cheap
        # lifecycle read, and its poll_after_ms is the authoritative pacing hint.
        # Pin the same workspace the lookup used — jobs are per-workspace.
        action = RepairAction(
            next_step="call_tool",
            tool="claude_job_status",
            arguments={"job_id": meta.get("job_id"), "workspace_root": bmeta.cwd},
        )
        retry_after_ms = poll_after_ms()
    elif code == "job_failed":
        # Free and argument-less: the readiness probe is the one mechanical step
        # that distinguishes a broken install/auth from a one-off run failure.
        action = RepairAction(next_step="call_tool", tool="claude_status")
    return ErrorResult(
        error=ErrorInfo(
            code=cast("ErrorCode", code),
            message=message,
            repair=repair,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            details=ErrorDetails(field="job_id", value=str(meta.get("job_id") or "") or None),
            action=action,
        ),
        meta=bmeta,
    ).model_dump(mode="json", exclude_none=True)


# Retained for tests and any external callers that pre-generate ids the 0.7 way;
# no longer used by the server.
def new_job_id() -> str:
    return uuid4().hex
