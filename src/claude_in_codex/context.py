"""Gather git diff context for review. Claude never runs git itself."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field

from claude_in_codex.config import git_timeout_seconds
from claude_in_codex.schemas import ContextSummary

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


class GitUnavailableError(RuntimeError):
    """Raised when the git executable is missing or cannot be launched."""


class NotAGitRepoError(RuntimeError):
    """Raised when the selected workspace is not a git working tree."""


def _valid_ref(ref: str) -> bool:
    """A conservative git ref/commit check: no leading dash, no option/shell chars."""
    return bool(ref) and not ref.startswith("-") and bool(_REF_RE.match(ref))


SECRET_PATH_RE = re.compile(
    r"(^|/)(\.env(\.|$)|\.envrc$|\.netrc$|\.pypirc$|.*\.env$|.*\.pem$|.*\.key$|id_rsa|id_ed25519|.*\.p12$)",
    re.IGNORECASE,
)

# Single-token, high-confidence secret shapes. Each is anchored on a vendor prefix
# with enough trailing entropy that ordinary identifiers do not collide. Kept
# conservative on purpose: false positives garble otherwise-legitimate diffs.
SECRET_VALUE_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),  # GitHub classic token
    re.compile(r"github_pat_[0-9A-Za-z_]{22,}"),  # GitHub fine-grained PAT
    re.compile(r"glpat-[0-9A-Za-z_-]{20,}"),  # GitLab personal access token
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),  # Slack token
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic API key
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),  # OpenAI project key
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI classic key
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{16,}"),  # Stripe secret key
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),  # Google API key
    re.compile(r"npm_[A-Za-z0-9]{36}"),  # npm automation token
    re.compile(r"pypi-[A-Za-z0-9_-]{16,}"),  # PyPI upload token
    re.compile(r"eyJ[A-Za-z0-9_=-]{10,}\.eyJ[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=-]{8,}"),  # JWT
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)((?:(?:api|access|secret|private)?_?(?:key|token|secret)|passw(?:or)?d|pwd|passphrase)\s*[:=]\s*['\"]?)[A-Za-z0-9._~+/=-]{16,}"
    ),
    # Connection-string / URI userinfo password: keep the scheme + user + host so the
    # diff stays reviewable, drop only the password between ':' and '@'.
    re.compile(r"([a-z][a-z0-9+.-]*://[^\s:/@]*:)[^\s:/@]+(?=@)"),
]

# Multi-line key blocks (PEM/PKCS8/OpenSSH/PGP) are redacted statefully in _redact,
# not line-by-line, so the whole base64 body is dropped rather than only the BEGIN
# marker. Trailing "[A-Z0-9 ]*" covers "OPENSSH"/"RSA" prefixes and "PGP ... BLOCK".
_PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----")
_PRIVATE_KEY_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----")


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


def _is_not_git_repo_error(stderr: str) -> bool:
    return "not a git repository" in stderr.lower()


def _classify_git_failure(stderr: str) -> None:
    message = stderr.strip() or "git failed"
    if _is_not_git_repo_error(message):
        raise NotAGitRepoError(message)
    raise RuntimeError(message)


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


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
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise GitUnavailableError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        _classify_git_failure(proc.stderr)
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
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise GitUnavailableError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git rev-parse timed out after {timeout}s") from exc
    if proc.returncode != 0 and _is_not_git_repo_error(proc.stderr):
        raise NotAGitRepoError(proc.stderr.strip() or "not a git repository")
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


def _split_diff_prefix(line: str) -> tuple[str, str]:
    """Split a scannable diff line into its +/-/space marker and its content.

    Lines without a diff marker (e.g. a raw "Authorization:" header) yield an empty
    prefix so the whole line is treated as content.
    """
    if line.startswith(("+", "-", " ")) and not line.startswith(("+++", "---")):
        return line[0], line[1:]
    return "", line


_REDACTED = "[redacted: secret value]"


def _redact_key_content(content: str, in_block: bool) -> tuple[str, bool, bool]:
    """Redact PEM/OpenSSH/PGP key material within one content line.

    Handles markers that share a physical line (e.g. an escaped one-liner
    ``key="-----BEGIN...-----\\nMII...\\n-----END...-----"``) as well as true
    multi-line blocks. The BEGIN/END markers stay visible; only the body between
    them is dropped, and the open-block state never leaks past an inline END.

    Returns ``(emitted, changed, in_block_after)``.
    """
    if in_block:
        end = _PRIVATE_KEY_END_RE.search(content)
        if end is None:
            return _REDACTED, True, True  # still inside the block: drop the whole line
        # Body may precede the END marker on this closing line; keep END onward.
        head = content[: end.start()]
        emit = (_REDACTED if head.strip() else head) + content[end.start() :]
        return emit, True, False

    begin = _PRIVATE_KEY_BEGIN_RE.search(content)
    if begin is None:
        return content, False, False
    end = _PRIVATE_KEY_END_RE.search(content, begin.end())
    if end is not None:
        # Whole key inline on one line: redact between the markers, stay closed.
        emit = content[: begin.end()] + _REDACTED + content[end.start() :]
        return emit, True, False
    # Block opens here; redact any body trailing the BEGIN marker on this line.
    tail = content[begin.end() :]
    emit = content[: begin.end()] + (_REDACTED if tail.strip() else tail)
    return emit, True, True


def redact_text(text: str) -> tuple[str, bool]:
    """Best-effort secret redaction for free-form model output (prose).

    Shares the diff path's pattern set (``SECRET_VALUE_PATTERNS``) and the stateful
    PEM/OpenSSH/PGP key-block handling (``_redact_key_content``), but with no
    diff-prefix or file-header awareness — every line is treated as content. A
    multi-line key block stays open until its END marker (or end of text), so an
    unterminated block fails closed. Returns ``(scrubbed, changed)``; empty/None
    input passes through unchanged. Defense-in-depth, NOT a guarantee: a key split
    across separate fields is out of scope (see #66 / SECURITY.md).
    """
    if not text:
        return text, False
    out_lines: list[str] = []
    changed = False
    in_key_block = False
    # split("\n") (not splitlines) so \n-delimited prose round-trips exactly.
    for line in text.split("\n"):
        if in_key_block or _PRIVATE_KEY_BEGIN_RE.search(line):
            emit, key_changed, in_key_block = _redact_key_content(line, in_key_block)
            # The key branch preserves any prefix before BEGIN / suffix after END, so
            # still scan the emitted line for an unrelated token sharing that line.
            emit, value_changed = _redact_secret_values(emit)
            line_changed = key_changed or value_changed
        else:
            emit, line_changed = _redact_secret_values(line)
        changed = changed or line_changed
        out_lines.append(emit)
    return "\n".join(out_lines), changed


def redact_tree(value: object) -> object:
    """Deep-apply ``redact_text`` to every string in a nested list/dict/str.

    Used to scrub untrusted, model/CLI-derived structured payloads (e.g.
    ``permission_denials``) while preserving shape. Dict KEYS are redacted as well
    as values: this data is relayed verbatim into ``meta`` (which is not
    str()-coerced like the structured findings path), so a secret-shaped key would
    otherwise survive. Non-string leaves (ints, None, bools) are returned
    untouched."""
    if isinstance(value, str):
        return redact_text(value)[0]
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {redact_text(str(key))[0]: redact_tree(item) for key, item in value.items()}
    return value


def _redact(diff: str) -> tuple[str, list[str]]:
    out_lines: list[str] = []
    redacted: list[str] = []
    skipping = False
    in_key_block = False
    current_path = ""

    def note_redacted() -> None:
        if current_path and current_path not in redacted:
            redacted.append(current_path)

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            spec = line[len("diff --git ") :]  # "a/<path> b/<path>" (paths may be quoted)
            current_path = _diff_path_from_header(line)
            in_key_block = False  # never let a key block bleed across files
            skipping = bool(SECRET_PATH_RE.search(spec) or SECRET_PATH_RE.search(current_path))
            if skipping:
                redacted.append(current_path or spec)
                out_lines.append(line)  # keep the real header so reviewers see the file
                out_lines.append("[redacted: secret-looking file not sent]")
                continue
        if skipping:
            continue

        scan_line = (
            line.startswith(("+", "-", " ")) and not line.startswith(("+++", "---"))
        ) or line.startswith("Authorization:")
        if not scan_line:
            in_key_block = False  # hunk/metadata boundary ends any open block
            out_lines.append(line)
            continue

        prefix, content = _split_diff_prefix(line)
        if in_key_block or _PRIVATE_KEY_BEGIN_RE.search(content):
            emit_content, changed, in_key_block = _redact_key_content(content, in_key_block)
            if changed:
                note_redacted()
            out_lines.append(f"{prefix}{emit_content}")
            continue

        emit_content, changed = _redact_secret_values(content)
        if changed:
            note_redacted()
        out_lines.append(f"{prefix}{emit_content}")
    return "\n".join(out_lines), redacted


def gather_context(
    cwd: str, scope: str, base: str, paths: list[str] | None = None, head: str | None = None
) -> ContextResult:
    # Explicit head only makes sense for a base...head branch comparison; reject it
    # for working_tree/staged rather than silently ignoring it.
    if head is not None and scope != "branch":
        raise InvalidHeadError(f"head is only valid for scope=branch, not {scope!r}")
    # Coalesce only None (caller omitted head), never "" — an explicit empty string
    # must fall through to _valid_ref and raise invalid_head, not silently use HEAD.
    effective_head = "HEAD" if head is None else head
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
