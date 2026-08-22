"""Private background-job worker.

The worker owns the lifetime lock for a detached job and is the only process
that observes the child command's streams. It writes the child's stdout to
``--result-path`` ATOMICALLY (tmp + rename after the child exits), so the job
store's "done when result.json parses" contract can never observe a partial
envelope, and it sanitizes complete stderr lines before writing them to
``--stderr-path``, so the job store never persists raw diagnostics.

Invoked by the pontonier JobStore as ``cmd_factory(job_dir)`` output; the
store redirects THIS process's own stdout/stderr to the record's
``stderr.log`` (worker self-diagnostics only) and streams the prompt to our
stdin, which the child inherits — the prompt never lands on disk or argv.

The store runs THIS process with ``cwd=<job_dir>``. The child is spawned with
``cwd=--workspace`` instead, so a detached run sees the same working directory
as the equivalent synchronous run.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from claude_in_codex.context import SecretRedactor

try:
    fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

MAX_STDERR_LINE_BYTES = 65_536
_TRUNCATED_LINE = "[stderr line truncated]"


def _lock_worker(path: Path):
    """Open and exclusively lock ``path`` for the caller's lifetime.

    POSIX provides the cross-process ownership proof consumed by the job store.
    On platforms without fcntl, the file remains open but restart recovery fails
    closed because the parent cannot positively verify the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+b")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    if fcntl is None:  # pragma: no cover - non-POSIX fallback
        return lock_file
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def _write_redacted_stderr(stream, path: Path) -> None:
    redactor = SecretRedactor()
    pending = bytearray()
    overlong = False

    with path.open("w", encoding="utf-8") as output:
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            for byte in chunk:
                if byte == 0x0A:
                    if overlong:
                        output.write(f"{_TRUNCATED_LINE}\n")
                    else:
                        line = pending.decode("utf-8", errors="replace")
                        output.write(f"{redactor.redact_line(line)[0]}\n")
                    pending.clear()
                    overlong = False
                elif not overlong:
                    pending.append(byte)
                    if len(pending) > MAX_STDERR_LINE_BYTES:
                        pending.clear()
                        overlong = True
                        # We deliberately stop retaining the line, so we cannot
                        # know whether a key-block BEGIN marker appeared later in
                        # it. Fail closed for following lines until an END marker.
                        redactor.in_key_block = True
        if overlong:
            output.write(_TRUNCATED_LINE)
        elif pending:
            line = pending.decode("utf-8", errors="replace")
            output.write(redactor.redact_line(line)[0])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lock-path", required=True)
    parser.add_argument("--stderr-path", required=True)
    parser.add_argument("--result-path", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        return 127

    lock_file = _lock_worker(Path(args.lock_path))
    _ = lock_file  # keep the advisory lock alive until this process exits

    # A group SIGTERM is delivered to both worker and child. Keep the worker (and
    # therefore its ownership lock) alive while a TERM-resistant child may still
    # need verified SIGKILL escalation from the parent.
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda _signum, _frame: None)

    result_path = Path(args.result_path)
    tmp_path = result_path.with_name(result_path.name + ".tmp")
    try:
        with tmp_path.open("wb") as rf:
            with contextlib.suppress(OSError):
                tmp_path.chmod(0o600)
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=args.workspace,
                    stdin=None,  # inherit: the store streams the prompt to OUR stdin
                    stdout=rf,
                    stderr=subprocess.PIPE,
                )
            except OSError:
                Path(args.stderr_path).write_text(
                    "job command could not be started", encoding="utf-8"
                )
                return 127

            assert proc.stderr is not None
            _write_redacted_stderr(proc.stderr, Path(args.stderr_path))
            code = proc.wait()
    except OSError:
        return 127
    # Publish atomically only now: the store treats a parseable result.json as
    # "done", so a partially-streamed envelope must never be visible under the
    # final name. An empty/garbage stdout is still published — the store then
    # finalizes the record as "failed" (unparseable), same as before.
    with contextlib.suppress(OSError):
        tmp_path.replace(result_path)
    return code


if __name__ == "__main__":  # pragma: no branch
    raise SystemExit(main(sys.argv[1:]))
