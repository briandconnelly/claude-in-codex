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
same as 0.7. The SYSTEM prompt is a different matter and always has been: the
guardrails, and any caller `system_prompt_append` text composed behind them,
ride argv as the `--append-system-prompt` value, so they are visible to a
process listing for the run's duration. The server writes only the caller
text's FINGERPRINT (sha256 + byte length) to the job record, never the text
itself — but Claude's own output is stored in the record until it is consumed
or expires, and output can repeat any input, so a persona that asks Claude to
echo a phrase puts that phrase on disk. The same holds for `prompt` and
`context`. "Not written by the server" is the guarantee; "never on disk" is not.
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
from pydantic import ValidationError

from claude_in_codex.claude import contract_changed_error
from claude_in_codex.cli_contract import is_contract_drift
from claude_in_codex.config import MAX_FOCUS_BYTES, contains_framing_marker
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
    SystemPromptAppend,
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
# The store's idempotency index keys on (tool, key). All three *_async starters
# share ONE namespace, so a key is unique per workspace rather than per tool: the
# same key reused across two different starters is an idempotency_conflict (the
# arg_hash cannot match), never a cross-tool replay of a run the caller did not
# ask for. The value is frozen at the original name because changing it would
# orphan every live reservation on upgrade.
_IDEMPOTENCY_NAMESPACE = "claude_review_changes_async"


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
    # FINGERPRINT of the caller's system-prompt text, never the text. A job
    # result read later must be able to attest which prompt produced it, and the
    # sha256/byte-length does that; storing the prose would put system-prompt
    # material in the on-disk job record, which is exactly what
    # --no-session-persistence exists to prevent.
    system_prompt_append: SystemPromptAppend | None = None
    # The caller's `focus` text, stored VERBATIM (unlike system_prompt_append, which
    # is fingerprinted). A job result read in a later session must be able to report
    # that its verdict covers this focus only, and a digest cannot name the topic.
    # This is caller-authored prose on disk -- the same class of data as `paths`,
    # which the record already stores -- and it is bounded by the boundary's
    # MAX_FOCUS_BYTES cap. It is never system-prompt material and never argv.
    focus: str | None = None
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
            "system_prompt_append": (
                cfg.system_prompt_append.model_dump() if cfg.system_prompt_append else None
            ),
            # Always written, even when None, so _build_meta can tell a record that
            # was genuinely unfocused from one written before focus was persisted.
            "focus": cfg.focus or None,
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


class ClaudeExecutableError(OSError):
    """Marker: the launch failed on `claude` itself, not on the job-state directory.

    A launch can raise OSError from either, and the two need opposite repairs.
    Without this marker the server matched on FileNotFoundError/PermissionError
    and reported "install Claude Code" for BOTH — so an unwritable state
    directory sent the caller to reinstall a CLI that was already there. The two
    concrete classes keep Popen's exception types (see _check_executable), so
    callers matching those still match.
    """


class ClaudeExecutableMissing(ClaudeExecutableError, FileNotFoundError):
    """`claude` is not on PATH, or the named path does not exist."""


class ClaudeExecutableNotRunnable(ClaudeExecutableError, PermissionError):
    """`claude` exists but is not executable."""


def _on_path_but_not_executable(name: str) -> bool:
    """True when some PATH entry holds `name` as a file that cannot be executed.

    Only consulted once shutil.which() has already answered None, to tell its two
    causes apart; a readable candidate that merely lacks the execute bit is a
    chmod away from working, which is a different repair from "install it".
    """
    for entry in os.get_exec_path():
        candidate = Path(entry) / name
        try:
            if candidate.is_file() and not os.access(candidate, os.X_OK):
                return True
        except OSError:  # unreadable PATH entry: not our candidate, keep looking
            continue
    return False


def _check_executable(cmd: list[str], cwd: str) -> None:
    """Preserve Popen's immediate missing/non-executable command failure.

    The raised types stay FileNotFoundError/PermissionError for fidelity; the
    ClaudeExecutableError mixin only adds the provenance the server needs to
    pick a repair. An empty argv is deliberately NOT marked: that is a bug in
    this process, not a missing CLI, and it should reach the caller as
    internal_error rather than as advice to install something.
    """
    if not cmd:
        raise FileNotFoundError("job command is empty")
    executable = cmd[0]
    if Path(executable).name != executable:
        path = Path(executable)
        if not path.is_absolute():
            path = Path(cwd) / path
        if not path.is_file():
            raise ClaudeExecutableMissing(executable)
        if not os.access(path, os.X_OK):
            raise ClaudeExecutableNotRunnable(executable)
    elif shutil.which(executable) is None:
        # shutil.which() tests X_OK, so it answers None for "absent" AND for "on
        # PATH but not executable". The bare name is what production passes
        # (cli_contract.CLAUDE_BIN), so collapsing the two here would leave
        # ClaudeExecutableNotRunnable unreachable in every real launch, and the
        # chmod repair would never be the one a caller actually sees.
        if _on_path_but_not_executable(executable):
            raise ClaudeExecutableNotRunnable(executable)
        raise ClaudeExecutableMissing(executable)


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


def arg_hash_for(cmd: list[str], prompt: str | None, paths: list[str] | None = None) -> str:
    """The effective-argument digest a keyed launch is deduplicated under.

    Two launches with the same idempotency_key but different effective arguments
    are a conflict, not a replay. The triple (argv, prompt, paths) IS the effective
    argument set: argv carries model, effort, budget, access, and config-mode
    flags, and the prompt carries scope, base, head, focus, and the gathered diff.
    Hashing them directly means a newly added tool parameter cannot silently fall
    out of the digest the way an enumerated field list allowed — `focus`, `model`,
    and `reasoning_effort` all did.

    `paths` is passed separately because it is the one effective argument with no
    carrier of its own. It never reaches argv (it rides GIT's argv, not Claude's),
    and #141 removed it from the prompt, where it used to be interpolated in the
    server's own voice. Carrying it here rather than restoring it to the prompt
    keeps both properties: the values stay out of what Claude reads, and two
    filters remain distinguishable to the index.

    Nothing else can stand in for it. The filter is applied when the diff is
    gathered, so distinct filters that select the SAME changes — `["src"]` and
    `["src/file.py"]` when only that file changed — produce an identical prompt
    and identical argv. Without this argument they hash alike, and a keyed retry
    that narrowed or widened its filter silently receives the earlier job's
    answer, carrying the earlier job's `meta.paths`. That is precisely the
    misattributed paid answer the (key, effective arguments) guarantee exists to
    refuse. Caught by Copilot reviewing #147.

    Volatile bookkeeping (timeouts, workspace provenance, redaction counts) is
    excluded by construction: it appears in neither argv nor the prompt.

    `detail` is excluded the same way, and that exclusion is deliberate rather
    than incidental. It selects how a stored result is RENDERED, not what Claude
    is asked or paid to do, and the record keeps the raw envelope — so a replayed
    job can still be read at any density by passing `detail` to claude_job_result,
    for free. Treating it as an effective argument would turn a free re-render
    into an idempotency_conflict and push the caller into a second paid run to get
    a rendering they could already have. Published in
    claude_capabilities.async_lifecycle and pinned by a test, so it is a contract,
    not an accident of what happens to reach argv.
    """
    material = json.dumps(
        {"argv": list(cmd), "prompt": prompt, "paths": list(paths) if paths else None},
        sort_keys=True,
    )
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
            tool=_IDEMPOTENCY_NAMESPACE,
            key=key,
            arg_hash=arg_hash_for(cmd, stdin_text, cfg.paths),
            extra=_extra_for(cfg, cwd),
            stdin_text=stdin_text,
        )


# ------------------------------------------------------- legacy keyed launches
# 0.7 keyed launches published idem-<sha16>.json markers in the workspace dir.
# They are still read and reaped (staleness), but never written — and reading one
# no longer replays it. A 0.7 marker carries no argument digest, so it cannot
# prove a retry matches the job it names; the server detects it and refuses with
# idempotency_conflict. See server._legacy_keyed_job.


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


def find_live_job_for_key(cwd: str, key: str) -> str | None:
    """The job_id an existing record reserved for this idempotency_key, or None.

    Advisory, and deliberately NOT the dedupe path: start_job_idempotent still
    owns the atomic (key, arg_hash) reservation, and nothing here should be used
    to decide whether to spawn.

    It exists for the one caller that must answer "does this key already hold a
    paid job?" WITHOUT being able to build the prompt the digest needs — the
    empty-diff branch, which returns before any launch is attempted. Without it
    a keyed retry whose diff has since gone empty reports "no changes" while the
    job that key reserved keeps running and keeps spending.

    KNOWN LIMIT, and not one that locking fixes. A concurrent launch that has
    gathered a non-empty diff but has not yet reserved the key is invisible here,
    so an empty-diff caller can still answer "no changes" moments before that
    peer spawns. The race is read-before-write: no atomicity on THIS read can
    observe a reservation that does not exist yet. Closing it needs the empty-diff
    outcome to TAKE the reservation, so the two serialize on the key — which needs
    a reserve-without-spawn primitive the store does not expose (see #131). What
    this does cover is the sequential case the published retry guidance actually
    describes: launch, lose the connection, commit, retry.
    """
    with _JOBS_LOCK:
        for sd in _store().list_jobs(cwd):
            job_id = sd.get("job_id", "")
            try:
                jd = _job_dir(cwd, job_id)
            except ValueError:
                continue
            meta = _read_meta(jd)
            if meta is not None and _record_extra(meta).get("idempotency_key") == key:
                return job_id
    return None


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
    fingerprint, fp_warning = _fingerprint_from(c.get("system_prompt_append"))
    security_warnings = list(c.get("security_warnings") or [])
    if fp_warning:
        security_warnings.append(fp_warning)
    # Key ABSENT means the record predates focus persistence, so whether the review
    # was narrowed is unknowable; key present and None means it genuinely was not.
    # Collapsing the two would report a possibly-narrowed pass as a full-review pass.
    # Gated on the kind that HAS a focus parameter: consult and adversarial review
    # could never be narrowed, so warning about them raises a doubt that cannot arise,
    # and a warning that fires where it cannot matter is one a reader learns to skip.
    focus, focus_warning = _focus_from(c, meta.get("kind"))
    if focus_warning:
        security_warnings.append(focus_warning)
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
        security_warnings=security_warnings,
        elapsed_ms=_elapsed_ms(meta),
        system_prompt_append=fingerprint,
        focus=focus,
        job_id=meta.get("job_id"),
    )


# The tools that accept a `focus`; the others cannot be narrowed, so an absent focus
# on their records is not an ambiguity.
_FOCUSABLE_KINDS = {"claude_review_changes"}


def _focus_from(config: dict, kind: object) -> tuple[str | None, str | None]:
    """Rebuild the review's focus from an on-disk record.

    Returns (focus, warning). Three cases have to stay distinct, because collapsing
    any two of them reports a narrowed verdict as a full-review one:

    * key ABSENT -- the record predates focus persistence, so the narrowing is
      unknowable. Only ambiguous for a kind that HAS a focus parameter: consult and
      adversarial review could never be narrowed, and a warning that fires where it
      cannot matter is one a reader learns to skip.
    * key present and None -- the review genuinely ran unfocused.
    * key present and not a string, over MAX_FOCUS_BYTES, or carrying a framing
      marker -- the record is tampered, truncated, or hand-written, because the live
      boundary (`server._validate_focus`) never accepts the last two. Like a malformed
      persona fingerprint (`_fingerprint_from`), this must degrade to "unknown" rather
      than turn a result read into an unstructured exception; the record is an
      ordinary local file that any process can edit. The same cap and marker refusal
      apply here so a record cannot replay into meta what the boundary refused: an
      unbounded string, or text posing as the server's own framing.
    """
    if "focus" not in config:
        return None, UNKNOWN_FOCUS_WARNING if kind in _FOCUSABLE_KINDS else None
    raw = config["focus"]
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, MALFORMED_FOCUS_WARNING
    # "replace", not strict: a lone surrogate in a hand-edited record must count
    # toward the ceiling, not raise out of a result read.
    if len(raw.encode("utf-8", "replace")) > MAX_FOCUS_BYTES or contains_framing_marker(raw):
        return None, MALFORMED_FOCUS_WARNING
    return raw or None, None


MALFORMED_FOCUS_WARNING = (
    "The job record's focus is malformed, so meta cannot attest what this review was "
    "narrowed to; treat the absent focus as unknown, not as a full-review verdict."
)

UNKNOWN_FOCUS_WARNING = (
    "This job record predates focus recording, so meta cannot attest whether the "
    "review was narrowed by a focus; treat the absent focus as unknown, not as a "
    "full-review verdict."
)

MALFORMED_FINGERPRINT_WARNING = (
    "The job record's system_prompt_append fingerprint is malformed, so meta "
    "cannot attest which system prompt this run used; treat the absent "
    "fingerprint as unknown, not as the default prompt."
)


def _fingerprint_from(raw: object) -> tuple[SystemPromptAppend | None, str | None]:
    """Rebuild the persona fingerprint from an on-disk record.

    Returns (fingerprint, warning). The record is a file another process wrote
    and anyone can edit: a tampered, truncated, or hand-written value must not
    turn a status read into an unstructured error — but it must not be silent
    either, because an absent fingerprint on a completed run means "the
    guardrail prompt ran alone", and a malformed one means "unknown". The
    warning carries that distinction into `security_warnings`."""
    if raw is None:
        return None, None
    try:
        return SystemPromptAppend.model_validate(raw), None
    except (ValidationError, TypeError, ValueError):
        return None, MALFORMED_FINGERPRINT_WARNING


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
        # (started_epoch, summary): the sort key rides alongside the payload
        # rather than inside it, so it cannot leak into the wire schema.
        summaries: list[tuple[float, dict]] = []
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
                (
                    meta.get("started_epoch", 0.0),
                    {
                        "job_id": job_id,
                        "kind": meta.get("kind", ""),
                        "status": state,
                        "started_at": meta.get("started_at", ""),
                        "elapsed_ms": _elapsed_ms(meta),
                        "result_available": state == "done",
                        "expires_at": _expires_at(meta),
                        "cost_usd": _terminal_cost(jd, state),
                    },
                )
            )
        summaries.sort(key=lambda pair: pair[0], reverse=True)  # newest first
        return {
            "ok": True,
            "jobs": [summary for _, summary in summaries],
            "fingerprint": FINGERPRINT,
        }


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
