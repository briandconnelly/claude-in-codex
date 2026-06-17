"""Gather git diff context for review. Claude never runs git itself."""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field

from cc_plugin_codex.config import git_timeout_seconds
from cc_plugin_codex.schemas import ContextSummary

MAX_DIFF_BYTES = 200_000

_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class InvalidScopeError(ValueError):
    """Raised when the requested diff scope is not recognized."""


class InvalidBaseError(ValueError):
    """Raised when the base ref for scope=branch is malformed/unsafe."""


class InvalidHeadError(ValueError):
    """Raised when the head ref for scope=branch is malformed/unsafe or unresolvable."""


class InvalidPathsError(ValueError):
    """Raised when one or more git pathspec filters are malformed/unsafe."""


def _valid_ref(ref: str) -> bool:
    """A conservative git ref/commit check: no leading dash, no option/shell chars."""
    return bool(ref) and not ref.startswith("-") and bool(_REF_RE.match(ref))


SECRET_PATH_RE = re.compile(
    r"(^|/)(\.env(\.|$)|\.envrc$|\.netrc$|\.pypirc$|.*\.env$|.*\.pem$|.*\.key$|id_rsa|id_ed25519|.*\.p12$)",
    re.IGNORECASE,
)

SECRET_VALUE_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)((?:(?:api|access|secret|private)?_?(?:key|token|secret)|passw(?:or)?d|pwd|passphrase)\s*[:=]\s*['\"]?)[A-Za-z0-9._~+/=-]{16,}"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


@dataclass
class ContextResult:
    text: str
    summary: ContextSummary
    truncated: bool = False
    truncation_hint: str | None = None
    redacted_paths: list[str] = field(default_factory=list)
    diff_bytes: int = 0  # full (pre-truncation) UTF-8 byte size of the redacted diff


@dataclass(frozen=True)
class DiffOptions:
    scope: str
    base: str
    paths: list[str] | None = None
    head: str = "HEAD"


def normalize_paths(paths: list[str] | None) -> list[str] | None:
    """Validate path filters before they reach git argv."""
    if not paths:
        return None
    normalized: list[str] = []
    for path in paths:
        if path == "":
            raise InvalidPathsError("paths entries must not be empty")
        if path.startswith("-"):
            raise InvalidPathsError(f"path must not start with '-': {path!r}")
        if path.startswith(":"):
            raise InvalidPathsError(f"git pathspec magic is not supported: {path!r}")
        if "\\" in path:
            raise InvalidPathsError(f"path must use '/' separators: {path!r}")
        if path.startswith("/"):
            raise InvalidPathsError(f"path must be repo-relative: {path!r}")
        if _WINDOWS_DRIVE_RE.match(path):
            raise InvalidPathsError(f"path must be repo-relative: {path!r}")
        if any(segment == ".." for segment in path.split("/")):
            raise InvalidPathsError(f"path must not contain '..' segments: {path!r}")
        normalized.append(path)
    return normalized


def _git(cwd: str, *args: str) -> str:
    timeout = git_timeout_seconds()
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git failed")
    return proc.stdout


def _ref_exists(cwd: str, ref: str) -> bool:
    """Whether ref resolves to a commit.

    Syntactically safe but nonexistent refs should be reported as invalid_base or
    invalid_head, not as a generic git/internal failure. This keeps branch-diff
    tools repairable for agents.
    """
    timeout = git_timeout_seconds()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git rev-parse timed out after {timeout}s") from exc
    return proc.returncode == 0


def _diff_args(opts: DiffOptions) -> list[str]:
    # --no-ext-diff + --no-textconv prevent configured external/textconv diff drivers
    # from executing commands during our own git call.
    common = ["diff", "--no-ext-diff", "--no-textconv"]
    if opts.scope == "working_tree":
        args = common
    elif opts.scope == "staged":
        args = [*common, "--cached"]
    elif opts.scope == "branch":
        base = opts.base
        if not _valid_ref(base):
            raise InvalidBaseError(f"invalid base ref: {base!r}")
        head = opts.head
        if not _valid_ref(head):
            raise InvalidHeadError(f"invalid head ref: {head!r}")
        # --end-of-options ensures the refs can never be parsed as git options.
        args = [*common, "--end-of-options", f"{base}...{head}"]
    else:
        raise InvalidScopeError(f"invalid scope: {opts.scope}")
    if opts.paths:
        args = [*args, "--", *opts.paths]
    return args


def _summary(cwd: str, diff_args: list[str]) -> ContextSummary:
    summary_args = list(diff_args)
    summary_args.insert(1, "--numstat")
    numstat = _git(cwd, *summary_args)
    files = added = removed = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        files += 1
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            removed += int(parts[1])
    return ContextSummary(files_changed=files, lines_added=added, lines_removed=removed)


def _diff_path_from_header(line: str) -> str:
    spec = line[len("diff --git ") :]
    try:
        parts = shlex.split(spec)
    except ValueError:
        parts = spec.split()
    if len(parts) >= 2:
        path = parts[1]
        return path[2:] if path.startswith("b/") else path
    return spec


def _redact_secret_values(line: str) -> tuple[str, bool]:
    redacted = False
    out = line
    for pattern in SECRET_VALUE_PATTERNS:

        def repl(match: re.Match) -> str:
            nonlocal redacted
            redacted = True
            if match.lastindex:
                return f"{match.group(1)}[redacted: secret value]"
            return "[redacted: secret value]"

        out = pattern.sub(repl, out)
    return out, redacted


def _redact(diff: str) -> tuple[str, list[str]]:
    out_lines: list[str] = []
    redacted: list[str] = []
    skipping = False
    current_path = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            spec = line[len("diff --git ") :]  # "a/<path> b/<path>" (paths may be quoted)
            current_path = _diff_path_from_header(line)
            skipping = bool(SECRET_PATH_RE.search(spec) or SECRET_PATH_RE.search(current_path))
            if skipping:
                redacted.append(current_path or spec)
                out_lines.append(line)  # keep the real header so reviewers see the file
                out_lines.append("[redacted: secret-looking file not sent]")
                continue
        if not skipping:
            scan_line = (
                line.startswith(("+", "-", " ")) and not line.startswith(("+++", "---"))
            ) or line.startswith("Authorization:")
            emit = line
            if scan_line:
                emit, changed = _redact_secret_values(line)
                if changed and current_path and current_path not in redacted:
                    redacted.append(current_path)
            out_lines.append(emit)
    return "\n".join(out_lines), redacted


def gather_context(
    cwd: str, scope: str, base: str, paths: list[str] | None = None, head: str | None = None
) -> ContextResult:
    # Explicit head only makes sense for a base...head branch comparison; reject it
    # for working_tree/staged rather than silently ignoring it.
    if head is not None and scope != "branch":
        raise InvalidHeadError(f"head is only valid for scope=branch, not {scope!r}")
    effective_head = head or "HEAD"
    opts = DiffOptions(scope=scope, base=base, paths=normalize_paths(paths), head=effective_head)
    diff_args = _diff_args(opts)  # raises InvalidScopeError/InvalidBaseError/InvalidHeadError
    if scope == "branch":
        if not _ref_exists(cwd, base):
            raise InvalidBaseError(f"base ref does not resolve to a commit: {base!r}")
        if not _ref_exists(cwd, effective_head):
            raise InvalidHeadError(f"head ref does not resolve to a commit: {effective_head!r}")
    summary = _summary(cwd, diff_args)
    raw = _git(cwd, *diff_args)
    text, redacted = _redact(raw)
    truncated = False
    hint = None
    encoded = text.encode("utf-8", "replace")
    diff_bytes = len(encoded)  # the true size, reported even when we truncate below
    if diff_bytes > MAX_DIFF_BYTES:
        text = encoded[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
        truncated = True
        hint = (
            f"diff exceeded {MAX_DIFF_BYTES} bytes; retry with paths=[...], use "
            "scope=staged, choose a closer branch base, or call claude_ask with "
            "selected context"
        )
    return ContextResult(
        text=text,
        summary=summary,
        truncated=truncated,
        truncation_hint=hint,
        redacted_paths=redacted,
        diff_bytes=diff_bytes,
    )
