"""Background-job lifecycle tests.

These drive jobs.start_job with a fake command (not the real `claude`) that writes
a known JSON envelope, so the full start -> status -> result/cancel/timeout flow is
exercised deterministically and for free.
"""

import json
import time

import pytest

from cc_plugin_codex import jobs
from cc_plugin_codex.jobs import JobConfig

_INNER = {
    "summary": "off-by-one bug", "verdict": "concerns", "confidence": "high",
    "findings": [{"severity": "high", "title": "subtraction", "file": "app.py",
                  "line": 2, "evidence": "a - b", "risk": "wrong", "recommendation": "use +"}],
    "questions": [], "assumptions": [],
}
_ENVELOPE = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": json.dumps(_INNER), "session_id": "sess-1",
    "total_cost_usd": 0.0123, "usage": {"input_tokens": 100, "output_tokens": 50},
})


def _cfg(**over):
    base = dict(kind="claude_review_changes", config_mode="inherit", access="toolless",
                scope="working_tree", base="main", detail="summary",
                timeout_seconds=1800, workspace_source="cwd", context_summary=None)
    base.update(over)
    return JobConfig(**base)


def _emit_cmd(envelope=_ENVELOPE):
    # `printf %s "$0"` writes the envelope (passed as $0) to stdout -> result.json.
    return ["sh", "-c", "printf '%s' \"$0\"", envelope]


def _sleep_cmd(seconds=30):
    return ["sh", "-c", f"sleep {seconds}"]


def _drift_cmd(message="error: unknown option '--effort'"):
    # Write a contract-drift signature to stderr and leave stdout (result.json)
    # empty, so the job is "failed" with a drift-bearing stderr tail.
    return ["sh", "-c", "printf '%s' \"$0\" 1>&2; exit 2", message]


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_PLUGIN_CODEX_STATE_DIR", str(tmp_path / "state"))


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


def test_job_meta_carries_requested_budget_and_warning(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(
        _emit_cmd(), cwd,
        _cfg(workspace_source="cwd", requested_max_budget_usd=0.30))
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["meta"]["requested_max_budget_usd"] == 0.30
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


def test_job_timeout_on_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_PLUGIN_CODEX_JOB_MAX_SECONDS", "0")  # deadline = start time
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_sleep_cmd(), cwd, _cfg())
    st = jobs.status(cwd, job_id)  # first poll past deadline reaps it
    assert st["status"] == "timeout"
    payload, _ = jobs.result(cwd, job_id)
    assert payload["error"]["code"] == "job_timeout"


def test_job_not_found(tmp_path):
    cwd = str(tmp_path)
    assert jobs.status(cwd, "nope") is None
    assert jobs.cancel(cwd, "nope") is None
    payload, found = jobs.result(cwd, "nope")
    assert found is False


def test_terminal_job_reaped_after_ttl(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    _await_done(cwd, job_id)
    # TTL of 0 means a terminal record is eligible for cleanup on the next call.
    monkeypatch.setenv("CC_PLUGIN_CODEX_JOB_TTL", "0")
    time.sleep(0.02)
    assert jobs.status(cwd, job_id) is None  # reaped


def test_result_preserves_record_by_default(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(_emit_cmd(), cwd, _cfg())
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True and payload["ok"] is True
    assert jobs.status(cwd, job_id)["status"] == "done"


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


def test_failed_job_without_drift_stays_job_failed(tmp_path):
    cwd = str(tmp_path)
    job_id, _ = jobs.start_job(
        ["sh", "-c", "printf 'boom' 1>&2; exit 1"], cwd, _cfg())
    _await_done(cwd, job_id)
    payload, found = jobs.result(cwd, job_id)
    assert found is True
    assert payload["error"]["code"] == "job_failed"
