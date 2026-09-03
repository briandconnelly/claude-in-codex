"""Gather git diff context for review. Claude never runs git itself."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field

from pontonier.core import redaction as _redaction

from claude_in_codex.config import git_timeout_seconds, unencodable_reason
from claude_in_codex.schemas import ContextSummary, bounded_repr

MAX_DIFF_BYTES = 200_000

# How many path-filter entries this server will probe individually for #149's
# per-entry match counts. Each probe is its own `git diff --name-only`, and the
# `paths` parameter has no maxItems, so an unbounded caller list would otherwise
# turn one review into arbitrarily many git invocations. Above the cap the counts
# are reported as None -- absent, never guessed. 32 covers every plausible hand-
# written filter while keeping the worst case bounded.
MAX_PATH_MATCH_PROBES = 32

# Wall-clock budget for the whole probe pass. The count cap above bounds how MANY
# probes run, not how long they take: each is its own git process under
# git_timeout_seconds (60s by default), so a count cap alone permits 32x that in
# the worst case -- a real amplification of the single gather the caller asked
# for. This is what actually bounds it, and it is enforced by handing each probe
# the REMAINING budget as its own process timeout. Checking the deadline only
# between probes would not: one slow git could then run a full
# git_timeout_seconds past the budget and still return counts, which is the
# difference between bounding when probes start and bounding what the pass
# costs. Over budget the counts are reported absent
# rather than partially, because a partial list would still be positionally
# aligned with `paths` and a caller reading a zero could not tell "selected
# nothing" from "never measured". Generous next to a measured ~5ms per probe on an
# ordinary repo, so it bites only on the pathological case it exists for.
MAX_PATH_MATCH_SECONDS = 5.0

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


class GitTimeoutError(RuntimeError):
    """Raised when a git subprocess exceeded its timeout.

    A RuntimeError subclass so every existing `except RuntimeError` around a
    gather keeps behaving exactly as before. It exists so ONE caller can tell a
    timeout apart from a git failure: the path-match probes, which degrade to
    "counts absent" on a timeout instead of failing the whole review (#155)."""


def _valid_ref(ref: str) -> bool:
    """A conservative git ref/commit check: no leading dash, no option/shell chars."""
    return bool(ref) and not ref.startswith("-") and bool(_REF_RE.match(ref))


# The redaction ENGINE is pontonier's (`pontonier.core.redaction`) — this bridge's
# local engine was retired once upstream reached parity-or-better on every local
# behavior: stateful key-block handling, the full vendor-pattern set (including
# github_pat_/glpat_/sk-ant-/npm_/pypi-, upstreamed from here), and a streaming
# line redactor for the job worker. What the shared engine ADDS over the old
# local one: span-merge with `[redacted: possibly partial secret value]` honesty
# markers, quoted/bracketed labelled keys, richer connection-string coverage,
# and the source-file code-reference exemption. These names re-export so callers
# and tests keep one import site.
SECRET_PATH_RE = _redaction.SECRET_PATH_RE
SECRET_VALUE_PATTERNS = _redaction.SECRET_VALUE_PATTERNS
SecretRedactor = _redaction.StreamRedactor

# Every Unicode Cc code point. `sanitize_echo_prose` applies the same strip before
# redacting, but only to a whole finished string; the streaming stderr path needs
# it per line, BEFORE the stateful line redactor sees the line, so a key block
# spanning several lines still redacts. Mirrors the shared policy rather than
# importing its private regex.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def strip_control_chars(line: str) -> str:
    """Delete Cc code points from ONE line, before it is redacted.

    A control character wedged into a credential splits it, so the redaction
    patterns miss and the secret survives. Stripping first makes the run
    contiguous again. Stripping AFTER redaction would be worse than useless: it
    would reassemble a secret the redactor had already declined to mask.
    """
    return _CONTROL_CHARS_RE.sub("", line)


@dataclass
class ContextResult:
    text: str
    summary: ContextSummary
    truncated: bool = False
    truncation_hint: str | None = None
    redacted_paths: list[str] = field(default_factory=list)
    diff_bytes: int = 0  # full (pre-truncation) UTF-8 byte size of the redacted diff
    # Per-entry file counts for the caller's path filter, positionally aligned
    # with the `paths` argument. None when there was no filter, when the list
    # exceeded MAX_PATH_MATCH_PROBES, or when the pass ran out of
    # MAX_PATH_MATCH_SECONDS. A zero marks an entry that selected nothing
    # -- a typo the caller cannot otherwise see, since `meta.paths` echoes their
    # list back and so agrees with it (#149).
    path_match_counts: list[int] | None = None


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
    """The environment for every git call: C locale, and no inherited GIT_* state.

    Git's own environment variables override repository discovery. GIT_DIR,
    GIT_WORK_TREE, GIT_INDEX_FILE and friends make git operate on whatever
    repository the parent process named, silently ignoring the cwd we resolved
    and validated. A server launched from a git hook, or from any parent that
    exports them, would then read a different repository's diff and send it to a
    paid external API — the workspace guarantee broken with no error.

    Every GIT_* name is dropped rather than a denylist of the dangerous ones: the
    set grows across git versions, and no git call here needs inherited GIT_*
    state (all are read-only, local, and fully specified by argv and cwd). An
    unrecognized GIT_* variable must not be able to redirect us."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
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
            raise InvalidPathsError(f"path must not start with '-': {bounded_repr(path)}")
        if path.startswith(":"):
            raise InvalidPathsError(f"git pathspec magic is not supported: {bounded_repr(path)}")
        if "\\" in path:
            raise InvalidPathsError(f"path must use '/' separators: {bounded_repr(path)}")
        if path.startswith("/"):
            raise InvalidPathsError(f"path must be repo-relative: {bounded_repr(path)}")
        if _WINDOWS_DRIVE_RE.match(path):
            raise InvalidPathsError(f"path must be repo-relative: {bounded_repr(path)}")
        if any(segment == ".." for segment in path.split("/")):
            raise InvalidPathsError(f"path must not contain '..' segments: {bounded_repr(path)}")
        # Not a policy rule like the ones above: git argv is encoded strictly, so a
        # path with no UTF-8 encoding raises UnicodeEncodeError inside subprocess,
        # outside this module's error taxonomy. Refuse it here, where the caller
        # still gets invalid_paths (#140).
        if unencodable_reason(path) is not None:
            raise InvalidPathsError(
                f"path is not valid UTF-8 (lone surrogate): {bounded_repr(path)}"
            )
        normalized.append(path)
    return normalized


def _git(cwd: str, *args: str, timeout: float | None = None) -> str:
    """Run git, capturing stdout.

    `timeout` overrides the configured per-process budget DOWNWARD for callers
    that own a tighter deadline than a single process (the path-match probes).
    It is never raised above `git_timeout_seconds()`, so an override can only
    make a call give up sooner.
    """
    configured = git_timeout_seconds()
    timeout = configured if timeout is None else min(configured, timeout)
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
        raise GitTimeoutError(f"git {' '.join(args)} timed out after {timeout}s") from exc
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
            raise InvalidBaseError(f"invalid base ref: {bounded_repr(base)}")
        head = opts.head
        if not _valid_ref(head):
            raise InvalidHeadError(f"invalid head ref: {bounded_repr(head)}")
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


def redact_text(text: str) -> tuple[str, bool]:
    """Best-effort secret redaction for free-form model output (prose).

    Thin wrapper over the shared engine (`pontonier.core.redaction.redact_text`),
    keeping this bridge's historical `(scrubbed, changed)` tuple shape. The engine
    applies the inline value patterns AND the stateful key-block pass, failing
    closed on an unterminated block. `changed` reports whether the text was
    rewritten at all. Defense-in-depth, NOT a guarantee: a key split across
    separate fields is out of scope (see #66 / SECURITY.md).
    """
    if not text:
        return text, False
    out = _redaction.redact_text(text) or ""
    return out, out != text


def sanitize_echo_prose(text: str) -> str:
    """Sanitize foreign multi-line text bound for an agent-visible envelope.

    Thin wrapper over `pontonier.core.redaction.sanitize_echo_prose`. Use this,
    NOT `redact_text`, wherever text this server did not author is echoed into
    an error message: subprocess stderr, job diagnostics, model result text.

    It deletes Unicode Cc code points BEFORE redacting. The order is fixed
    inside the shared function and is not this caller's to choose — redacting
    first leaves a control-character-split secret untouched, and stripping
    afterwards then reassembles it in the outgoing text. Stripping also removes
    terminal escapes, which would otherwise recolor, reposition, or erase the
    agent's view of the error. It does not truncate; callers apply their own
    bound after the call.
    """
    if not text:
        return text
    return _redaction.sanitize_echo_prose(text) or ""


def redact_tree(value: object) -> object:
    """Deep-apply ``redact_text`` to every string in a nested list/dict/str.

    Used to scrub untrusted, model/CLI-derived structured payloads (e.g.
    ``permission_denials``) while preserving shape. Dict KEYS are redacted as well
    as values — a DELIBERATE divergence from pontonier's own ``redact_tree``: this
    data is relayed verbatim into ``meta`` (which is not str()-coerced like the
    structured findings path), so a secret-shaped key would otherwise survive.
    Non-string leaves (ints, None, bools) are returned untouched."""
    if isinstance(value, str):
        return redact_text(value)[0]
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {redact_text(str(key))[0]: redact_tree(item) for key, item in value.items()}
    return value


def _redact(diff: str) -> tuple[str, list[str]]:
    """Redact secret-looking files and inline values in a unified diff.

    Delegates to the shared engine (`pontonier.core.redaction.redact`), which keeps
    every property the local engine had — withheld secret-path hunks behind their
    visible headers, stateful key-block handling that never bleeds across file or
    hunk boundaries, `Authorization:` header scanning, and trailing-newline
    preservation so the redacted patch still applies — and adds span-merge partial
    markers plus the source-file code-reference exemption.
    """
    return _redaction.redact(diff)


def _path_match_counts(cwd: str, opts: DiffOptions) -> list[int] | None:
    """How many files each path-filter entry selected, one count per entry.

    Asked of git rather than derived from the gathered diff. Git's pathspec
    matching is exact-or-directory-prefix until an entry contains a wildcard, at
    which point it is fnmatch; reimplementing that here to attribute an already-
    gathered file list back to entries would be a second, divergent copy of a
    subtlety this module has no reason to own. One `--name-only` diff per entry
    asks the authority instead.

    Measured on the FULL diff for that entry, so the byte cap applied afterwards
    cannot turn a matched entry into a reported zero.
    """
    if not opts.paths or len(opts.paths) > MAX_PATH_MATCH_PROBES:
        return None
    deadline = time.monotonic() + MAX_PATH_MATCH_SECONDS
    counts: list[int] = []
    for path in opts.paths:
        # The remaining budget becomes the probe's own process timeout. Checking
        # the deadline only BETWEEN probes would let a single slow git run to
        # git_timeout_seconds (60s by default) past a 5s budget and still return
        # counts -- the budget would bound how many probes start, not how long
        # the pass takes, which is not what it claims to do.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        args = _diff_args(DiffOptions(scope=opts.scope, base=opts.base, head=opts.head))
        args.insert(1, "--name-only")
        try:
            out = _git(cwd, *args, "--", path, timeout=remaining)
        except GitTimeoutError:
            # Absent counts, not a failed gather. The probes are an extra the
            # caller did not ask for; the diff they DID ask for is gathered
            # below and must not be lost to a measurement running long.
            return None
        counts.append(sum(1 for line in out.splitlines() if line.strip()))
    return counts


def gather_context(
    cwd: str,
    scope: str,
    base: str,
    paths: list[str] | None = None,
    head: str | None = None,
    measure_paths: bool = True,
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
            raise InvalidBaseError(f"base ref does not resolve to a commit: {bounded_repr(base)}")
        if not _ref_exists(cwd, effective_head):
            raise InvalidHeadError(
                f"head ref does not resolve to a commit: {bounded_repr(effective_head)}"
            )
    summary = _summary(cwd, diff_args)
    path_match_counts = _path_match_counts(cwd, opts) if measure_paths else None
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
            "scope=staged, choose a closer branch base, or call claude_consult with "
            "selected context"
        )
    return ContextResult(
        text=text,
        summary=summary,
        truncated=truncated,
        truncation_hint=hint,
        redacted_paths=redacted,
        diff_bytes=diff_bytes,
        path_match_counts=path_match_counts,
    )
