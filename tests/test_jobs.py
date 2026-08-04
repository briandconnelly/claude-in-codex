"""Background-job lifecycle tests.

These drive jobs.start_job with a fake command (not the real `claude`) that writes
a known JSON envelope, so the full start -> status -> result/cancel/timeout flow is
exercised deterministically and for free.
"""

import errno
import io
import json
import os
import pathlib
import signal
import time

import anyio
import pytest

from claude_in_codex import _job_worker, jobs
from claude_in_codex.jobs import JobConfig
from claude_in_codex.schemas import OUTPUT_BOUNDS

_INNER = {
    "summary": "off-by-one bug",
    "verdict": "concerns",
    "confidence": "high",
    "findings": [
        {
            "severity": "high",
            "title": "subtraction",
            "file": "app.py",
            "line": 2,
            "evidence": "a - b",
            "risk": "wrong",
            "recommendation": "use +",
        }
    ],
    "questions": [],
    "assumptions": [],
}
_ENVELOPE = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(_INNER),
        "session_id": "sess-1",
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
)


def _cfg(**over):
    base = dict(
        kind="claude_review_changes",
        config_mode="inherit",
        access="toolless",
        scope="working_tree",
        base="main",
        head=None,
        detail="summary",
        timeout_seconds=1800,
        workspace_source="cwd",
        context_summary=None,
    )
    base.update(over)
    return JobConfig(**base)


def _emit_cmd(envelope=_ENVELOPE):
    # `printf %s "$0"` writes the envelope (passed as $0) to stdout -> result.json.
    return ["sh", "-c", "printf '%s' \"$0\"", envelope]


def _sleep_cmd(seconds=30):
    return ["sh", "-c", f"sleep {seconds}"]


def _emit_after_cmd(seconds=0.1, envelope=_ENVELOPE):
    return ["sh", "-c", f"sleep {seconds}; printf '%s' \"$0\"", envelope]


def _drift_cmd(message="error: unknown option '--effort'"):
    # Write a contract-drift signature to stderr and leave stdout (result.json)
    # empty, so the job is "failed" with a drift-bearing stderr tail.
    return ["sh", "-c", "printf '%s' \"$0\" 1>&2; exit 2", message]


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))


def _await_done(cwd, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = jobs.status(cwd, job_id)
        if st and st["status"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("job did not leave running state in time")


def test_job_done_returns_normalized_result(tmp_path):
    cwd = str(tmp_path)
    job_id, started_at = jobs.start_job(_emit_cmd(), cwd, _cfg())
    assert started_at
    st = _await_done(cwd, job_id)
    assert st["status"] == "done"
    assert st["result_available"] is True
    assert st["cost_usd"] == 0.0123
    # Status output conforms to the published contract: carries the fingerprint and
    # reports the deadline window the job started with (1800s), not a live env read.
    assert st["fingerprint"]
    assert st["deadline_seconds"] == 1800
    assert st["poll_after_ms"] == 1000
    assert st["ttl_seconds"] == 86400
    assert st["expires_at"]

    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["ok"] is True
    assert payload["verdict"] == "concerns"
    assert payload["meta"]["job_id"] == job_id
    assert payload["meta"]["cost_usd"] == 0.0123


def test_job_result_detail_re_renders_the_stored_envelope(tmp_path):
    """A truncated background summary is recoverable at full detail for free (#94).

    The stored artifact is the raw claude envelope, not a rendered result, so the
    same record answers both densities without another paid call."""
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg(detail="summary"))
    _await_done(cwd, job_id)

    summary, found = jobs.result(cwd, job_id)
    assert found is True
    assert "text" not in summary["raw_response"]  # the job's own level still applies

    full, found = jobs.result(cwd, job_id, detail="full")
    assert found is True
    assert full["raw_response"]["text"]
    assert full["verdict"] == summary["verdict"]
    # Non-destructive: the override does not rewrite the record's own level.
    again, _ = jobs.result(cwd, job_id)
    assert "text" not in again["raw_response"]


def test_consume_renders_full_by_default_so_deletion_loses_nothing(tmp_path):
    """Deletion is irreversible, so the last read must hand back everything (#94).

    Consuming at the job's summary level would delete the stored envelope while
    the truncation block advertised a free re-read of the record just destroyed."""
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg(detail="summary"))
    _await_done(cwd, job_id)

    payload, found = jobs.result(cwd, job_id, consume=True)
    assert found is True
    assert payload["raw_response"]["text"]  # full detail, despite the job's summary level
    # And the record really is gone, so nothing may point back at it.
    _, still_there = jobs.result(cwd, job_id)
    assert still_there is False


def test_explicit_summary_consume_never_advertises_the_deleted_record(tmp_path):
    """A caller can still opt into the cheap final read — but the next step it
    gets back must be the paid re-run, not a call that can only 404."""
    cwd = str(tmp_path)
    inner = {
        "summary": "x",
        "verdict": "concerns",
        "confidence": "high",
        "findings": [
            {
                "severity": "low",
                "title": f"t{i}",
                "evidence": "e",
                "risk": "r",
                "recommendation": "rec",
            }
            for i in range(OUTPUT_BOUNDS["summary"].max_findings + 3)
        ],
    }
    envelope = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(inner)}
    )
    job_id, _ = jobs.start_job(_emit_cmd(envelope), cwd, _cfg(detail="summary"))
    _await_done(cwd, job_id)

    payload, found = jobs.result(cwd, job_id, consume=True, detail="summary")
    assert found is True
    assert payload["truncation"]["next_step"] == "retry_with_changes"
    assert payload["truncation"]["tool"] == "claude_review_changes"
    assert "arguments" not in payload["truncation"]
    _, still_there = jobs.result(cwd, job_id)
    assert still_there is False


def test_start_job_sends_stdin_without_argv_prompt(tmp_path):
    cwd = str(tmp_path)
    cmd = ["sh", "-c", "cat"]
    job_id, _ = jobs.start_job(cmd, cwd, _cfg(), stdin_text=_ENVELOPE)
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["ok"] is True
    assert _ENVELOPE not in cmd


def test_start_job_spawn_failure_cleans_partial_record(tmp_path):
    cwd = str(tmp_path)
    with pytest.raises(OSError):
        jobs.start_job(["definitely-no-such-claude-binary-xyz"], cwd, _cfg())
    assert jobs.list_jobs(cwd)["jobs"] == []


def test_job_config_persists_head(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg(scope="branch", base="main", head="feature"))
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["meta"]["head"] == "feature"
    # diff_range is recomputed from base+head, not persisted separately.
    assert payload["meta"]["diff_range"] == "main...feature"


def test_job_meta_defaults_head_for_branch_scope(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg(scope="branch", base="main"))
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["meta"]["head"] == "HEAD"
    assert payload["meta"]["diff_range"] == "main...HEAD"


def test_job_meta_non_branch_leaves_head_and_range_unset(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg(scope="working_tree", base="main"))
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["meta"].get("head") is None
    assert payload["meta"].get("diff_range") is None


def test_job_meta_carries_configured_and_effective_budget_and_warning(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(
        _emit_cmd(),
        cwd,
        _cfg(
            workspace_source="cwd",
            configured_max_budget_usd=99.0,
            effective_max_budget_usd=5.0,
        ),
    )
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["meta"]["configured_max_budget_usd"] == 99.0
    assert payload["meta"]["effective_max_budget_usd"] == 5.0
    # workspace_source=cwd must surface the footgun warning on the rebuilt job meta.
    assert "workspace_root" in payload["meta"]["workspace_warning"]


def test_terminal_nondone_job_surfaces_cost(tmp_path):
    # A cancelled/timeout job can still have left a cost-bearing envelope. status()
    # and list_jobs() must surface that spend, matching the result path and the
    # JobStatus.cost_usd contract ("terminal jobs that spent"), not only done jobs.
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    _await_done(cwd, job_id)
    # Simulate a cancel that raced in after the envelope landed: the envelope (with
    # its cost) is on disk, but the record is marked terminal-cancelled.
    jd = jobs._job_dir(cwd, job_id)
    meta = jobs._read_meta(jd)
    meta["terminal_status"] = "cancelled"
    jobs._write_meta(jd, meta)

    st = jobs.status(cwd, job_id)
    assert st["status"] == "cancelled"
    assert st["cost_usd"] == 0.0123

    listing = jobs.list_jobs(cwd)
    job = next(j for j in listing["jobs"] if j["job_id"] == job_id)
    assert job["status"] == "cancelled"
    assert job["cost_usd"] == 0.0123


def test_job_running_then_result_says_job_running(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_sleep_cmd(), cwd, _cfg())
    st = jobs.status(cwd, job_id)
    assert st["status"] == "running"
    assert st["result_available"] is False

    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["ok"] is False
    assert payload["error"]["code"] == "job_running"
    assert payload["error"]["retryable"] is True
    jobs.cancel(cwd, job_id)  # clean up the sleeper


def test_job_cancel(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_sleep_cmd(), cwd, _cfg())
    assert jobs.status(cwd, job_id)["status"] == "running"
    st = jobs.cancel(cwd, job_id)
    assert st["status"] == "cancelled"

    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["error"]["code"] == "job_cancelled"


def test_cancel_result_race_prefers_completed_envelope(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    job_id = "e" * 32
    jd = jobs._job_dir(cwd, job_id)
    jd.mkdir(parents=True)
    meta = {
        "job_id": job_id,
        "kind": "claude_review_changes",
        "started_epoch": time.time(),
        "started_at": "now",
        "deadline_epoch": time.time() + 10,
        "completed_epoch": None,
        "terminal_status": None,
        "config": {},
    }
    jobs._write_meta(jd, meta)
    monkeypatch.setattr(jobs, "_read_live_job", lambda *_args: (jd, meta, "running"))

    def finish(_jd, _meta):
        (jd / "result.json").write_text("{}")

    monkeypatch.setattr(jobs, "_terminate_verified", finish)
    assert jobs.cancel(cwd, job_id)["status"] == "done"
    assert meta["terminal_status"] is None


def test_job_timeout_on_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_MAX_SECONDS", "0")  # deadline = start time
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_sleep_cmd(), cwd, _cfg())
    st = jobs.status(cwd, job_id)  # first poll past deadline reaps it
    assert st["status"] == "timeout"
    payload, _ = jobs.result(cwd, job_id)
    assert payload["error"]["code"] == "job_timeout"


def test_deadline_result_race_prefers_completed_envelope(tmp_path, monkeypatch):
    jd = tmp_path / "job"
    jd.mkdir()
    meta = {
        "pid": 1234,
        "started_epoch": time.time() - 10,
        "deadline_epoch": time.time() - 1,
        "completed_epoch": None,
        "terminal_status": None,
    }
    monkeypatch.setattr(jobs, "_job_running", lambda *_args: True)

    def finish(_jd, _meta):
        (jd / "result.json").write_text("{}")

    monkeypatch.setattr(jobs, "_terminate_verified", finish)
    assert jobs._status_of(jd, meta) == "done"
    assert meta["terminal_status"] is None
    assert meta["completed_epoch"] is not None


def test_job_not_found(tmp_path):
    cwd = str(tmp_path)
    missing = "d" * 32
    assert jobs.status(cwd, missing) is None
    assert jobs.cancel(cwd, missing) is None
    payload, found = jobs.result(cwd, missing)
    assert found is False


@pytest.mark.parametrize("job_id", ["../outside", "/tmp/outside", "A" * 32, "a" * 31])
def test_internal_job_lookup_rejects_noncanonical_ids(tmp_path, job_id):
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        jobs.status(str(tmp_path), job_id)


def test_terminal_job_reaped_after_ttl(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    _await_done(cwd, job_id)
    # TTL of 0 means a terminal record is eligible for cleanup on the next call.
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_TTL", "0")
    time.sleep(0.02)
    assert jobs.status(cwd, job_id) is None  # reaped


def test_result_preserves_record_by_default(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True and payload["ok"] is True
    assert jobs.status(cwd, job_id)["status"] == "done"


async def test_concurrent_lifecycle_calls_do_not_hang(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_after_cmd(), cwd, _cfg())

    async def poll_status():
        seen = []
        for _ in range(20):
            st = await anyio.to_thread.run_sync(lambda: jobs.status(cwd, job_id))
            if st:
                seen.append(st["status"])
                if st["status"] == "done":
                    return seen
            await anyio.sleep(0.02)
        return seen

    async def poll_result():
        last = None
        for _ in range(20):
            payload, found = await anyio.to_thread.run_sync(lambda: jobs.result(cwd, job_id))
            assert found is True
            last = payload
            if payload["ok"] is True:
                return payload
            assert payload["error"]["code"] == "job_running"
            await anyio.sleep(0.02)
        return last

    async def poll_list():
        last = None
        for _ in range(20):
            last = await anyio.to_thread.run_sync(lambda: jobs.list_jobs(cwd))
            assert last["ok"] is True
            await anyio.sleep(0.02)
        return last

    outputs = {}

    async def store(key, fn):
        outputs[key] = await fn()

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tg:
            tg.start_soon(store, "statuses", poll_status)
            tg.start_soon(store, "result", poll_result)
            tg.start_soon(store, "listing", poll_list)

    assert "done" in outputs["statuses"]
    assert outputs["result"]["ok"] is True
    assert outputs["result"]["meta"]["job_id"] == job_id
    assert any(j["job_id"] == job_id for j in outputs["listing"]["jobs"])


def test_consume_deletes_record(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id, consume=True)
    assert found is True and payload["ok"] is True
    assert jobs.status(cwd, job_id) is None  # gone after consume


def test_failed_job_with_drift_stderr_is_cli_contract_changed(tmp_path):
    # The async twin of the sync cli_contract_changed path: a job that exits
    # nonzero with an unknown-flag stderr must classify as cli_contract_changed,
    # not a generic job_failed.
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_drift_cmd(), cwd, _cfg())
    st = _await_done(cwd, job_id)
    assert st["status"] == "failed"
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["ok"] is False
    assert payload["error"]["code"] == "cli_contract_changed"


def test_job_failed_is_not_retryable_and_names_the_readiness_probe(tmp_path):
    """A terminal record never becomes ok:true, so re-fetching it is not a retry.

    retryable=True here would loop an agent on a fetch that returns job_failed
    forever; the recoverable action is to diagnose, then launch a new job."""
    job_id, _ = jobs.start_job(["sh", "-c", "exit 3"], str(tmp_path), _cfg())
    _await_done(str(tmp_path), job_id)
    payload, found = jobs.result(str(tmp_path), job_id)
    assert found
    err = payload["error"]
    assert err["code"] == "job_failed"
    assert err["retryable"] is False
    assert "retry_after_ms" not in err
    assert err["action"] == {"next_step": "call_tool", "tool": "claude_status"}


def test_failed_job_without_drift_stays_job_failed(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(["sh", "-c", "printf 'boom' 1>&2; exit 1"], cwd, _cfg())
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["error"]["code"] == "job_failed"


def test_failed_job_persists_only_sanitized_stderr(tmp_path):
    cwd = str(tmp_path)
    secret = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    cmd = ["sh", "-c", "printf '%s' \"$0\" 1>&2; exit 1", f"failure token={secret}"]
    job_id, _ = jobs.start_job(cmd, cwd, _cfg())
    status = _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    stored = (jobs._job_dir(cwd, job_id) / "stderr.log").read_text()
    meta = jobs._read_meta(jobs._job_dir(cwd, job_id))

    assert found is True
    assert meta["stderr_sanitized"] is True
    assert secret not in stored
    assert secret not in json.dumps(status)
    assert secret not in json.dumps(payload)
    assert "[redacted: secret value]" in stored


def test_worker_redacts_multiline_keys_across_streamed_lines(tmp_path):
    body = b"MIIEvQIBADANBgkqSECRETKEYBODYdeadbeef0123456789"
    begin = b"-" * 5 + b"BEGIN RSA " + b"PRIVATE KEY" + b"-" * 5
    end = b"-" * 5 + b"END RSA " + b"PRIVATE KEY" + b"-" * 5
    stream = io.BytesIO(begin + b"\n" + body + b"\n" + end + b"\n")
    path = tmp_path / "stderr.log"
    _job_worker._write_redacted_stderr(stream, path)
    stored = path.read_text()
    assert body.decode() not in stored
    assert "[redacted: secret value]" in stored
    assert "BEGIN RSA " + "PRIVATE KEY" in stored
    assert "END RSA " + "PRIVATE KEY" in stored


def test_worker_discards_overlong_stderr_line(tmp_path):
    secret = b"ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    stream = io.BytesIO(secret + b"x" * _job_worker.MAX_STDERR_LINE_BYTES + b"\nnext\n")
    path = tmp_path / "stderr.log"
    _job_worker._write_redacted_stderr(stream, path)
    stored = path.read_text()
    assert secret.decode() not in stored
    assert stored == "[stderr line truncated]\n[redacted: secret value]\n"


def test_worker_discards_overlong_unterminated_stderr_line(tmp_path):
    stream = io.BytesIO(b"x" * (_job_worker.MAX_STDERR_LINE_BYTES + 1))
    path = tmp_path / "stderr.log"
    _job_worker._write_redacted_stderr(stream, path)
    assert path.read_text() == "[stderr line truncated]"


def test_worker_main_sanitizes_stderr_and_returns_child_status(tmp_path, monkeypatch):
    secret = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    observed = {}

    class Proc:
        stderr = io.BytesIO(f"failed with {secret}".encode())

        def wait(self):
            return 7

    def fake_popen(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr(_job_worker.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(_job_worker.signal, "signal", lambda *_args: None)
    lock_path = tmp_path / "worker.lock"
    stderr_path = tmp_path / "stderr.log"
    status = _job_worker.main(
        [
            "--lock-path",
            str(lock_path),
            "--stderr-path",
            str(stderr_path),
            "--",
            "fake-claude",
        ]
    )

    assert status == 7
    assert observed["command"] == ["fake-claude"]
    assert observed["kwargs"]["stderr"] is _job_worker.subprocess.PIPE
    assert secret not in stderr_path.read_text()
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_worker_main_rejects_empty_command(tmp_path):
    assert (
        _job_worker.main(
            [
                "--lock-path",
                str(tmp_path / "worker.lock"),
                "--stderr-path",
                str(tmp_path / "stderr.log"),
            ]
        )
        == 127
    )


def test_worker_main_records_generic_spawn_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        _job_worker.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("sensitive detail")),
    )
    monkeypatch.setattr(_job_worker.signal, "signal", lambda *_args: None)
    stderr_path = tmp_path / "stderr.log"
    status = _job_worker.main(
        [
            "--lock-path",
            str(tmp_path / "worker.lock"),
            "--stderr-path",
            str(stderr_path),
            "fake-claude",
        ]
    )
    assert status == 127
    assert stderr_path.read_text() == "job command could not be started"


def test_state_root_defaults_under_home(monkeypatch):
    monkeypatch.delenv(jobs.STATE_ENV, raising=False)
    assert jobs._state_root().parts[-3:] == (".cache", "claude-in-codex", "jobs")


def test_pid_helpers_handle_missing_pid():
    assert jobs._pid_alive(None) is False
    assert jobs._is_running(None) is False


def test_owned_pid_that_is_no_longer_waitable_is_not_reused(monkeypatch):
    pid = 4321
    jobs._OWNED_PIDS.add(pid)
    monkeypatch.setattr(jobs.os, "waitpid", lambda *_args: (_ for _ in ()).throw(ChildProcessError))
    monkeypatch.setattr(jobs.os, "kill", lambda *_args: pytest.fail("must not probe reused PID"))
    assert jobs._is_running(pid) is False
    assert pid not in jobs._OWNED_PIDS


def test_pid_alive_permission_error_means_alive(monkeypatch):
    def _raise(pid, sig):
        raise PermissionError

    monkeypatch.setattr(jobs.os, "kill", _raise)
    assert jobs._pid_alive(4321) is True


def test_pid_alive_when_signal_succeeds(monkeypatch):
    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: None)
    assert jobs._pid_alive(4321) is True


def test_is_running_oserror_returns_false(monkeypatch):
    def _raise(pid, flags):
        raise OSError

    monkeypatch.setattr(jobs.os, "waitpid", _raise)
    assert jobs._is_running(4321) is False


def test_signal_job_swallows_errors(monkeypatch):
    monkeypatch.setattr(jobs.os, "getpgid", lambda p: p)
    monkeypatch.setattr(jobs.os, "killpg", lambda *a: (_ for _ in ()).throw(ProcessLookupError))
    jobs._signal_job(4321, signal.SIGKILL)  # must not raise


def test_signal_job_falls_back_to_single_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs.os, "getpgid", lambda _pid: 9999)
    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    jobs._signal_job(4321, signal.SIGTERM)
    assert calls == [(4321, signal.SIGTERM)]


def test_terminate_verified_escalates_only_after_recheck(tmp_path, monkeypatch):
    pid = 4321
    signals = []
    jobs._OWNED_PIDS.add(pid)
    monkeypatch.setattr(jobs, "_job_running", lambda *_args: True)
    monkeypatch.setattr(jobs, "_TERMINATE_GRACE_SECONDS", 0)
    monkeypatch.setattr(jobs, "_signal_job", lambda _pid, sig: signals.append(sig))
    monkeypatch.setattr(jobs.os, "waitpid", lambda *_args: (pid, 0))
    jobs._terminate_verified(tmp_path, {"pid": pid})
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert pid not in jobs._OWNED_PIDS


def test_read_envelope_missing_empty_malformed_and_nondict(tmp_path):
    jd = tmp_path / "job"
    jd.mkdir()
    assert jobs._read_envelope(jd) is None  # no result.json (OSError)
    (jd / "result.json").write_text("")
    assert jobs._read_envelope(jd) is None  # empty
    (jd / "result.json").write_text("{not json")
    assert jobs._read_envelope(jd) is None  # malformed
    (jd / "result.json").write_text("[1, 2]")
    assert jobs._read_envelope(jd) is None  # not a dict


def test_read_meta_rejects_job_id_mismatch_and_non_dict(tmp_path):
    jd = tmp_path / ("a" * 32)
    jd.mkdir()
    (jd / "meta.json").write_text(json.dumps({"job_id": "b" * 32}))
    assert jobs._read_meta(jd) is None
    (jd / "meta.json").write_text("[]")
    assert jobs._read_meta(jd) is None


def test_unlocked_worker_lock_is_not_an_ownership_proof(tmp_path):
    jd = tmp_path / "job"
    jd.mkdir()
    (jd / "worker.lock").touch()
    assert jobs._worker_lock_held(jd) is False


def test_stderr_tail_missing_returns_none(tmp_path):
    jd = tmp_path / "job"
    jd.mkdir()
    assert jobs._stderr_tail(jd, {"stderr_sanitized": True}) is None


def test_stderr_tail_withholds_legacy_unsanitized_log(tmp_path):
    jd = tmp_path / "job"
    jd.mkdir()
    secret = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    (jd / "stderr.log").write_text(secret)
    tail = jobs._stderr_tail(jd, {})
    assert tail == jobs._LEGACY_STDERR_WITHHELD
    assert secret not in tail


def test_deadline_seconds_falls_back_to_env():
    assert jobs._deadline_seconds({}) == jobs.max_seconds()


def test_rmtree_swallows_errors(tmp_path):
    jobs._rmtree(tmp_path / "does-not-exist")  # iterdir raises -> swallowed


def test_reap_and_list_skip_nondir_and_bad_meta(tmp_path):
    cwd = str(tmp_path)
    ws = jobs._ws_dir(cwd)
    ws.mkdir(parents=True)
    (ws / "loose-file").write_text("not a dir")  # non-dir entry skipped
    bad = ws / "badjob"
    bad.mkdir()
    (bad / "meta.json").write_text("{not json")  # unreadable meta skipped
    assert jobs.list_jobs(cwd)["jobs"] == []


def test_job_lookup_and_listing_do_not_follow_symlinked_job_dir(tmp_path):
    cwd = str(tmp_path)
    job_id = "a" * 32
    ws = jobs._ws_dir(cwd)
    ws.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "meta.json").write_text(json.dumps({"job_id": job_id}))
    try:
        (ws / job_id).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    assert jobs.status(cwd, job_id) is None
    assert jobs.list_jobs(cwd)["jobs"] == []


def test_unowned_pid_without_worker_lock_is_never_signalled(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    job_id = "b" * 32
    jd = jobs._job_dir(cwd, job_id)
    jd.mkdir(parents=True)
    now = time.time()
    jobs._write_meta(
        jd,
        {
            "job_id": job_id,
            "kind": "claude_review_changes",
            "pid": os.getpid(),
            "owner": "owner-from-an-earlier-server",
            "stderr_sanitized": True,
            "started_epoch": now,
            "started_at": "now",
            "deadline_epoch": now - 1,
            "completed_epoch": None,
            "terminal_status": None,
            "config": {},
        },
    )
    monkeypatch.setattr(
        jobs,
        "_signal_job",
        lambda *_args: pytest.fail("an unverified PID must never be signalled"),
    )

    status = jobs.cancel(cwd, job_id)
    assert status["status"] == "failed"
    assert jobs._pid_alive(os.getpid()) is True


def test_held_worker_lock_verifies_job_after_owner_restart(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_sleep_cmd(), cwd, _cfg())
    jd = jobs._job_dir(cwd, job_id)
    deadline = time.time() + 2
    while time.time() < deadline and jobs._worker_lock_held(jd) is not True:
        time.sleep(0.01)
    assert jobs._worker_lock_held(jd) is True

    meta = jobs._read_meta(jd)
    meta["owner"] = "owner-from-an-earlier-server"
    jobs._write_meta(jd, meta)
    jobs._OWNED_PIDS.discard(meta["pid"])
    assert jobs.status(cwd, job_id)["status"] == "running"
    assert jobs.cancel(cwd, job_id)["status"] == "cancelled"


def test_count_cap_evicts_oldest_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv(jobs.MAX_COUNT_ENV, "1")
    cwd = str(tmp_path)
    first, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    _await_done(cwd, first)
    second, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())  # cap check evicts `first`
    _await_done(cwd, second)
    ids = {j["job_id"] for j in jobs.list_jobs(cwd)["jobs"]}
    assert second in ids
    assert first not in ids


def test_start_job_survives_chmod_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError))
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    assert job_id
    _await_done(cwd, job_id)


def test_start_job_wrapper_spawn_failure_cleans_partial_record(tmp_path, monkeypatch):
    job_id = "c" * 32
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("wrapper failed")),
    )
    with pytest.raises(OSError, match="wrapper failed"):
        jobs.start_job(["sh", "-c", "true"], str(tmp_path), _cfg(), job_id=job_id)
    assert not (jobs._ws_dir(str(tmp_path)) / job_id).exists()


def test_check_executable_rejects_empty_missing_and_nonexecutable(tmp_path):
    with pytest.raises(FileNotFoundError, match="empty"):
        jobs._check_executable([], str(tmp_path))
    with pytest.raises(FileNotFoundError):
        jobs._check_executable(["./missing"], str(tmp_path))
    path = tmp_path / "not-executable"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o600)
    with pytest.raises(PermissionError):
        jobs._check_executable(["./not-executable"], str(tmp_path))


def test_reservation_rejects_noncanonical_candidate(tmp_path):
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        jobs.reserve_idempotency_key(str(tmp_path), "key", "../outside")


def test_terminal_nondone_result_surfaces_cost(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    _await_done(cwd, job_id)
    jd = jobs._job_dir(cwd, job_id)
    meta = jobs._read_meta(jd)
    meta["terminal_status"] = "cancelled"
    jobs._write_meta(jd, meta)
    payload, found = jobs.result(cwd, job_id)
    assert found and payload["ok"] is False
    assert payload["error"]["code"] == "job_cancelled"
    assert payload["meta"]["cost_usd"] == 0.0123  # envelope cost surfaced


def test_job_running_result_error_carries_repair_call(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    job_id, _ = jobs.start_job(_sleep_cmd(), str(tmp_path), _cfg())
    payload, found = jobs.result(str(tmp_path), job_id)
    assert found
    err = payload["error"]
    assert err["code"] == "job_running"
    assert err["action"]["tool"] == "claude_job_status"
    # The workspace is pinned: jobs are per-workspace, so a status call that
    # resolved a different workspace would report job_not_found.
    assert err["action"]["arguments"] == {"job_id": job_id, "workspace_root": str(tmp_path)}
    jobs.cancel(str(tmp_path), job_id)


def test_find_by_idempotency_key_matches_live_job(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    job_id, _ = jobs.start_job(_sleep_cmd(), str(tmp_path), _cfg(idempotency_key="key-1"))
    assert jobs.find_by_idempotency_key(str(tmp_path), "key-1") == job_id
    assert jobs.find_by_idempotency_key(str(tmp_path), "other-key") is None
    jobs.cancel(str(tmp_path), job_id)


def test_find_by_idempotency_key_ignores_keyless_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    job_id, _ = jobs.start_job(_sleep_cmd(), str(tmp_path), _cfg())
    assert jobs.find_by_idempotency_key(str(tmp_path), "key-1") is None
    jobs.cancel(str(tmp_path), job_id)


def test_reserve_idempotency_key_single_winner_across_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    import concurrent.futures

    results = []

    def attempt(candidate):
        holder = jobs.reserve_idempotency_key(str(tmp_path), "race-key", candidate)
        if holder is None:
            job_id, _ = jobs.start_job(
                _sleep_cmd(), str(tmp_path), _cfg(idempotency_key="race-key"), job_id=candidate
            )
            return ("won", job_id)
        return ("lost", holder)

    candidates = [f"{i:032x}" for i in range(8)]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, candidates))
    winners = [r for r in results if r[0] == "won"]
    assert len(winners) == 1
    winner_id = winners[0][1]
    assert all(r[1] == winner_id for r in results)
    jobs.cancel(str(tmp_path), winner_id)


def test_reserve_release_allows_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    assert jobs.reserve_idempotency_key(str(tmp_path), "k", "a" * 32) is None
    jobs.release_idempotency_key(str(tmp_path), "k", "a" * 32)
    assert jobs.reserve_idempotency_key(str(tmp_path), "k", "b" * 32) is None
    jobs.release_idempotency_key(str(tmp_path), "k", "b" * 32)


def test_release_only_removes_own_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    assert jobs.reserve_idempotency_key(str(tmp_path), "k", "a" * 32) is None
    jobs.release_idempotency_key(str(tmp_path), "k", "z" * 32)  # not the holder
    assert jobs.reserve_idempotency_key(str(tmp_path), "k", "b" * 32) == "a" * 32
    jobs.release_idempotency_key(str(tmp_path), "k", "a" * 32)


def _xproc_attempt(args):
    workspace, state_dir, candidate = args
    import os

    os.environ["CLAUDE_IN_CODEX_STATE_DIR"] = state_dir
    from claude_in_codex import jobs as j

    holder = j.reserve_idempotency_key(workspace, "xproc-key", candidate)
    if holder is None:
        job_id, _ = j.start_job(
            ["sh", "-c", "sleep 30"],
            workspace,
            JobConfig(
                kind="claude_review_changes",
                config_mode="inherit",
                access="toolless",
                scope="working_tree",
                base="main",
                head=None,
                detail="summary",
                timeout_seconds=1800,
                workspace_source="cwd",
                context_summary=None,
                idempotency_key="xproc-key",
            ),
            job_id=candidate,
        )
        return ("won", job_id)
    return ("lost", holder)


def test_reserve_idempotency_key_single_winner_across_processes(tmp_path):
    import concurrent.futures

    state_dir = str(tmp_path / "state")
    args = [(str(tmp_path), state_dir, f"{i + 16:032x}") for i in range(4)]
    with concurrent.futures.ProcessPoolExecutor(4) as pool:
        results = list(pool.map(_xproc_attempt, args))
    winners = [r for r in results if r[0] == "won"]
    assert len(winners) == 1
    assert all(r[1] == winners[0][1] for r in results)
    os.environ["CLAUDE_IN_CODEX_STATE_DIR"] = state_dir
    from claude_in_codex import jobs as j

    j.cancel(str(tmp_path), winners[0][1])
    os.environ.pop("CLAUDE_IN_CODEX_STATE_DIR", None)


def test_reserve_idempotency_key_replaces_corrupt_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    path = jobs._reservation_path(str(tmp_path), "corrupt-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert jobs.reserve_idempotency_key(str(tmp_path), "corrupt-key", "a" * 32) is None
    jobs.release_idempotency_key(str(tmp_path), "corrupt-key", "a" * 32)


def test_reserve_idempotency_key_replaces_stale_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_TTL", "0")
    path = jobs._reservation_path(str(tmp_path), "stale-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": "z" * 32, "created_epoch": time.time() - 10}))
    # "z"*32 has no job record and the marker is past the (zeroed) TTL: replaced.
    assert jobs.reserve_idempotency_key(str(tmp_path), "stale-key", "a" * 32) is None
    jobs.release_idempotency_key(str(tmp_path), "stale-key", "a" * 32)


def test_reserve_idempotency_key_retries_on_vanished_marker(tmp_path, monkeypatch):
    """A read that races a concurrent release/replace can see FileNotFoundError; the
    reservation must retry without unlinking (not treat it as a corrupt marker)."""
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_TTL", "0")
    path = jobs._reservation_path(str(tmp_path), "vanish-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": "z" * 32, "created_epoch": time.time() - 10}))

    original_read_text = pathlib.Path.read_text
    calls = {"n": 0}

    def flaky_read_text(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError()
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", flaky_read_text)
    assert jobs.reserve_idempotency_key(str(tmp_path), "vanish-key", "a" * 32) is None
    monkeypatch.setattr(pathlib.Path, "read_text", original_read_text)

    holder = json.loads(path.read_text())
    assert holder["job_id"] == "a" * 32


def test_reserve_idempotency_key_falls_back_without_hardlinks(tmp_path, monkeypatch):
    """os.link failing with something other than FileExistsError (e.g. a filesystem
    without hardlink support) degrades to a best-effort os.replace instead of
    failing the keyed launch outright."""
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))

    def no_hardlinks(_src, _dst):
        raise OSError(errno.EPERM, "hardlinks not supported")

    monkeypatch.setattr(jobs.os, "link", no_hardlinks)
    assert jobs.reserve_idempotency_key(str(tmp_path), "no-hardlink-key", "a" * 32) is None
    path = jobs._reservation_path(str(tmp_path), "no-hardlink-key")
    holder = json.loads(path.read_text())
    assert holder["job_id"] == "a" * 32
    # No leftover temp file from the fallback path.
    assert list(path.parent.glob(".*tmp")) == []


def test_release_idempotency_key_missing_marker_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    jobs.release_idempotency_key(str(tmp_path), "never-reserved", "a" * 32)


def test_reap_workspace_removes_stale_marker_with_dead_job(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_TTL", "0")
    path = jobs._reservation_path(str(tmp_path), "reap-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": "z" * 32, "created_epoch": time.time() - 10}))
    jobs.list_jobs(str(tmp_path))  # triggers _reap_workspace
    assert not path.exists()


def test_reap_workspace_keeps_marker_for_live_job(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    job_id, _ = jobs.start_job(_sleep_cmd(), str(tmp_path), _cfg(idempotency_key="reap-live-key"))
    assert jobs.reserve_idempotency_key(str(tmp_path), "reap-live-key", job_id) is None
    jobs.list_jobs(str(tmp_path))  # triggers _reap_workspace; marker's job is still alive
    path = jobs._reservation_path(str(tmp_path), "reap-live-key")
    assert path.exists()
    jobs.cancel(str(tmp_path), job_id)


def test_reap_workspace_removes_invalid_marker_within_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    path = jobs._reservation_path(str(tmp_path), "fresh-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": "z" * 32, "created_epoch": time.time()}))
    jobs.list_jobs(str(tmp_path))
    assert not path.exists()


def test_reap_workspace_keeps_expired_marker_for_live_job(tmp_path, monkeypatch):
    """Even past its TTL, a marker whose job record still exists is kept."""
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    job_id, _ = jobs.start_job(_sleep_cmd(), str(tmp_path), _cfg(idempotency_key="reap-old-key"))
    path = jobs._reservation_path(str(tmp_path), "reap-old-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": job_id, "created_epoch": time.time() - 10}))
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_TTL", "0")
    jobs.list_jobs(str(tmp_path))
    assert path.exists()
    jobs.cancel(str(tmp_path), job_id)


def test_reap_workspace_ignores_unparseable_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_TTL", "0")
    path = jobs._reservation_path(str(tmp_path), "garbage-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    jobs.list_jobs(str(tmp_path))
    assert path.exists()


def test_reap_workspace_ignores_non_dict_marker_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_IN_CODEX_JOB_TTL", "0")
    path = jobs._reservation_path(str(tmp_path), "list-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["not", "a", "dict"]))
    jobs.list_jobs(str(tmp_path))
    assert path.exists()
