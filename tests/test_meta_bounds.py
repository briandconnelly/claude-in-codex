"""The response envelope is bounded by the server, not by caller input (#162).

#150 bounded every error `message` that echoes caller input. `meta` still echoed
the caller's `paths`, `base` and `head` verbatim -- and composed `diff_range`
from base+head -- so both rejection AND success envelopes stayed an unbounded
function of the arguments. These tests pin the boundary: oversized selectors are
refused at the input edge, and no envelope carries an unbounded echo.
"""

import json

import pytest
from fastmcp import Client
from tests.conftest import structured

from claude_in_codex.config import (
    MAX_PATH_ENTRY_BYTES,
    MAX_PATHS_ENTRIES,
    MAX_PATHS_TOTAL_BYTES,
    MAX_REF_BYTES,
)
from claude_in_codex.server import mcp

# Generous next to a bounded envelope (~1 KB) and far under the ~11-21 KB the
# unbounded echo produced, so a regression cannot slip through as "still smallish".
ENVELOPE_CEILING_BYTES = 8_000


async def _call(tool, args):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return structured(result)


def _size(payload) -> int:
    return len(json.dumps(payload))


@pytest.mark.parametrize("tool", ["claude_review_changes", "claude_dry_run"])
async def test_an_oversized_paths_entry_is_refused_and_not_echoed(fake_claude, git_repo, tool):
    entry = "src/" + "a" * MAX_PATH_ENTRY_BYTES
    data = await _call(
        tool, {"scope": "working_tree", "paths": [entry], "workspace_root": str(git_repo)}
    )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_paths"
    assert data["error"]["details"]["limit_bytes"] == MAX_PATH_ENTRY_BYTES
    assert _size(data) < ENVELOPE_CEILING_BYTES
    assert entry not in json.dumps(data)


async def test_too_many_paths_entries_are_refused(fake_claude, git_repo):
    data = await _call(
        "claude_review_changes",
        {
            "scope": "working_tree",
            "paths": [f"p{i}" for i in range(MAX_PATHS_ENTRIES + 1)],
            "workspace_root": str(git_repo),
        },
    )
    assert data["error"]["code"] == "invalid_paths"
    assert data["error"]["details"]["limit"] == MAX_PATHS_ENTRIES
    assert _size(data) < ENVELOPE_CEILING_BYTES


async def test_paths_are_refused_on_aggregate_bytes_even_when_each_entry_fits(
    fake_claude, git_repo
):
    """Per-entry and entry-count caps alone still permit a multi-hundred-KB echo."""
    entry = "src/" + "a" * 500
    count = (MAX_PATHS_TOTAL_BYTES // len(entry)) + 2
    assert count <= MAX_PATHS_ENTRIES, "this case must not be caught by the count cap"
    data = await _call(
        "claude_review_changes",
        {"scope": "working_tree", "paths": [entry] * count, "workspace_root": str(git_repo)},
    )
    assert data["error"]["code"] == "invalid_paths"
    assert data["error"]["details"]["limit_bytes"] == MAX_PATHS_TOTAL_BYTES
    assert _size(data) < ENVELOPE_CEILING_BYTES


async def test_an_oversized_base_is_refused_and_never_amplified_into_diff_range(
    fake_claude, git_repo
):
    """`diff_range` is COMPOSED from base+head, so bounding the parts is not enough."""
    base = "b" * (MAX_REF_BYTES + 1)
    data = await _call(
        "claude_review_changes",
        {"scope": "branch", "base": base, "workspace_root": str(git_repo)},
    )
    assert data["error"]["code"] == "invalid_base"
    assert _size(data) < ENVELOPE_CEILING_BYTES
    assert base not in json.dumps(data)
    assert data["meta"].get("diff_range") is None


async def test_an_oversized_head_is_refused(fake_claude, git_repo):
    head = "h" * (MAX_REF_BYTES + 1)
    data = await _call(
        "claude_review_changes",
        {"scope": "branch", "base": "HEAD", "head": head, "workspace_root": str(git_repo)},
    )
    assert data["error"]["code"] == "invalid_head"
    assert _size(data) < ENVELOPE_CEILING_BYTES
    assert head not in json.dumps(data)


async def test_the_caps_are_measured_in_utf8_bytes_not_code_points(fake_claude, git_repo):
    """A 3-byte code point must not buy three times the cap."""
    entry = "src/" + "中" * MAX_PATH_ENTRY_BYTES  # 3 bytes each, under the cap in chars
    assert len(entry) < MAX_PATH_ENTRY_BYTES * 3
    data = await _call(
        "claude_review_changes",
        {"scope": "working_tree", "paths": [entry], "workspace_root": str(git_repo)},
    )
    assert data["error"]["code"] == "invalid_paths"
    assert _size(data) < ENVELOPE_CEILING_BYTES


async def test_a_value_at_the_cap_is_still_accepted(fake_claude, git_repo):
    """The instrument can surface a positive: the cap rejects only what exceeds it."""
    entry = "src/" + "a" * (MAX_PATH_ENTRY_BYTES - len("src/"))
    assert len(entry.encode()) == MAX_PATH_ENTRY_BYTES
    data = await _call(
        "claude_review_changes",
        {"scope": "working_tree", "paths": [entry], "workspace_root": str(git_repo)},
    )
    assert data["ok"] is True, data.get("error")
    # Not just accepted -- echoed back LITERALLY. An implementation that bounded the
    # envelope by quietly withholding valid selectors would pass an ok-only
    # assertion while turning a filtered review into one that reads as unfiltered.
    assert data["meta"]["paths"] == [entry]


async def test_a_successful_envelope_is_bounded_too(fake_claude, git_repo):
    """#162's point: a VALID oversized path was echoed the same way on success."""
    data = await _call(
        "claude_review_changes",
        {
            "scope": "working_tree",
            "paths": [f"src/f{i}" for i in range(MAX_PATHS_ENTRIES)],
            "workspace_root": str(git_repo),
        },
    )
    assert data["ok"] is True, data.get("error")
    assert _size(data) < MAX_PATHS_TOTAL_BYTES + ENVELOPE_CEILING_BYTES
    # Same point at the entry-count boundary: bounded AND unchanged.
    assert data["meta"]["paths"] == [f"src/f{i}" for i in range(MAX_PATHS_ENTRIES)]


async def test_paths_and_paths_matched_stay_index_aligned(fake_claude, git_repo):
    """#149's contract: nothing here may leave a truncated or dropped entry aligned
    against a count the caller would read as belonging to a different path."""
    data = await _call(
        "claude_dry_run",
        {"scope": "working_tree", "paths": ["src", "tests"], "workspace_root": str(git_repo)},
    )
    assert data["ok"] is True, data.get("error")
    assert data["paths"] == ["src", "tests"]
    assert len(data["paths_matched"]) == len(data["paths"])


def test_a_legacy_job_record_over_the_caps_degrades_instead_of_raising(tmp_path):
    """Records outlive the release that wrote them (TTL), and are editable local
    state. One carrying a selector the live boundary now refuses must still yield a
    readable -- and bounded -- meta, because the result it describes was paid for."""
    from claude_in_codex import jobs

    record = {
        "config": {
            "config_mode": "inherit",
            "access": "toolless",
            "scope": "branch",
            "base": "b" * (MAX_REF_BYTES + 1),
            "timeout_seconds": 1800,
            "workspace_source": "param",
            "cwd": str(tmp_path),
            "paths": ["src/" + "a" * MAX_PATH_ENTRY_BYTES],
            "paths_matched": [3],
        },
        "context_summary": None,
    }

    rebuilt = jobs._build_meta(record)

    assert rebuilt.base is None
    assert rebuilt.diff_range is None
    assert rebuilt.paths is None
    # Dropped WITH its path list: a surviving count would be aligned against
    # nothing, which is worse than an absent pair (#149).
    assert rebuilt.paths_matched is None
    assert len(json.dumps(rebuilt.model_dump(mode="json", exclude_none=True))) < (
        ENVELOPE_CEILING_BYTES
    )
    # The omission is stated, so it cannot be read as "the caller passed none".
    assert any("selector size caps" in w for w in rebuilt.security_warnings)


def test_a_job_record_within_the_caps_is_rebuilt_untouched(tmp_path):
    """The negative above is only evidence if the same path passes a normal record."""
    from claude_in_codex import jobs

    record = {
        "config": {
            "config_mode": "inherit",
            "access": "toolless",
            "scope": "branch",
            "base": "main",
            "timeout_seconds": 1800,
            "workspace_source": "param",
            "cwd": str(tmp_path),
            "paths": ["src", "tests"],
            "paths_matched": [3, 1],
        },
        "context_summary": None,
    }

    rebuilt = jobs._build_meta(record)

    assert rebuilt.base == "main"
    assert rebuilt.diff_range == "main...HEAD"
    assert rebuilt.paths == ["src", "tests"]
    assert rebuilt.paths_matched == [3, 1]
    assert rebuilt.security_warnings == []


# Every tool that takes caller selectors, with the arguments each additionally
# requires (the adversarial pair reviews a claim, so `paths`/`base` only attach a
# diff to it). Schemas are extra="forbid", so these cannot simply be passed to all.
_SELECTOR_TOOLS = {
    "claude_review_changes": {},
    "claude_adversarial_review": {"target": "ship it"},
    "claude_review_changes_async": {},
    "claude_adversarial_review_async": {"target": "ship it"},
    "claude_dry_run": {},
}


@pytest.mark.parametrize("tool", _SELECTOR_TOOLS)
async def test_no_tool_reaches_a_success_envelope_with_an_over_cap_selector(
    fake_claude, git_repo, tool
):
    """The invariant the withholding rests on.

    `bounded_selectors` withholds an over-cap value, and a withheld `paths` is
    indistinguishable in shape from "the caller passed no filter". That is only safe
    while an over-cap value can never reach a SUCCESS envelope -- on a rejection the
    error names the field, so nothing is ambiguous. Asserted per tool rather than
    argued from the one code path, because each tool orders its own validation."""
    data = await _call(
        tool,
        {
            "scope": "working_tree",
            "paths": ["src/" + "a" * MAX_PATH_ENTRY_BYTES],
            "workspace_root": str(git_repo),
            **_SELECTOR_TOOLS[tool],
        },
    )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_paths"
    assert _size(data) < ENVELOPE_CEILING_BYTES


@pytest.mark.parametrize("tool", _SELECTOR_TOOLS)
@pytest.mark.parametrize("scope", ["branch", "working_tree", "staged"])
async def test_no_tool_reaches_a_success_envelope_with_an_over_cap_ref(
    fake_claude, git_repo, tool, scope
):
    """Every scope, not just the one that resolves refs.

    `_valid_ref` is reached from `_diff_args`, which only resolves refs for
    scope=branch. So an over-cap `base` on a working_tree call used to be ACCEPTED
    and then withheld from the SUCCESS envelope -- `meta.base` absent where a normal
    call shows the ref, which is exactly the "none supplied" misreading this design
    turns on. The cap belongs to the argument, not to the scope it is used under."""
    data = await _call(
        tool,
        {
            "scope": scope,
            "base": "b" * (MAX_REF_BYTES + 1),
            "workspace_root": str(git_repo),
            **_SELECTOR_TOOLS[tool],
        },
    )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_base"
    assert _size(data) < ENVELOPE_CEILING_BYTES


@pytest.mark.parametrize(
    "tool",
    # The two `_async` starters are omitted deliberately: reaching a SUCCESS envelope
    # from them needs a `claude` binary on PATH (they preflight it before enqueuing),
    # so including them would pass on a developer machine and fail in CI with
    # claude_not_found -- a green that depends on the environment, not on the code.
    # They are covered for the refusal above, and their meta is built by the same
    # `_meta` this asserts on.
    ["claude_review_changes", "claude_adversarial_review", "claude_dry_run"],
)
async def test_a_ref_the_scope_ignores_is_still_echoed_when_it_fits(fake_claude, git_repo, tool):
    """The negative above is only evidence if a normal ignored `base` survives.

    A working_tree call does not use `base`, but it does echo it, and withholding
    must stay distinguishable from that -- which requires the ordinary case to keep
    showing the value."""
    data = await _call(
        tool,
        {
            "scope": "working_tree",
            "base": "main",
            "workspace_root": str(git_repo),
            **_SELECTOR_TOOLS[tool],
        },
    )
    assert data["ok"] is True, data.get("error")
    meta = data.get("meta", data)
    assert meta.get("base") == "main"


async def test_an_over_cap_ref_never_reaches_a_job_record(fake_claude, git_repo):
    """Because the ref cap was scope-gated, a live async call could STORE an over-cap
    `base`; the record rebuild would then withhold it and attach the legacy-record
    security warning -- to a paid, current-release result, for a value the review
    never used. The starter must refuse before anything is written."""
    data = await _call(
        "claude_review_changes_async",
        {
            "scope": "working_tree",
            "base": "b" * (MAX_REF_BYTES + 1),
            "workspace_root": str(git_repo),
        },
    )
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_base"


def test_a_record_whose_paths_matched_does_not_fit_its_paths_drops_both(tmp_path):
    """Bounding `paths` alone leaves `paths_matched` free.

    A record naming one path beside 50,000 counts is two failures at once: an
    envelope proportional to the record (measured at 150 KB before this check), and
    a #149 alignment that cannot be true. The live path cannot produce it --
    `_path_match_counts` returns either None or exactly one count per entry -- but
    the record it is rebuilt from is editable local state."""
    from claude_in_codex import jobs

    record = {
        "config": {
            "config_mode": "inherit",
            "access": "toolless",
            "scope": "working_tree",
            "timeout_seconds": 1800,
            "workspace_source": "param",
            "cwd": str(tmp_path),
            "paths": ["src"],
            "paths_matched": [7] * 50_000,
        },
        "context_summary": None,
    }

    rebuilt = jobs._build_meta(record)

    assert rebuilt.paths is None
    assert rebuilt.paths_matched is None
    assert any("selector size caps" in w for w in rebuilt.security_warnings)
    assert len(json.dumps(rebuilt.model_dump(mode="json", exclude_none=True))) < (
        ENVELOPE_CEILING_BYTES
    )


@pytest.mark.parametrize(
    ("paths", "paths_matched"),
    [
        (["src"], [1, 2]),  # more counts than paths
        (["src", "tests"], [1]),  # fewer
        (None, [3]),  # counts with no filter at all
        (["src"], [-1]),  # not a file count
        (["src"], [10**5000]),  # serializes unboundedly on its own
    ],
)
def test_misaligned_or_implausible_counts_are_withheld(tmp_path, paths, paths_matched):
    from claude_in_codex import jobs

    rebuilt = jobs._build_meta(
        {
            "config": {
                "config_mode": "inherit",
                "access": "toolless",
                "scope": "working_tree",
                "timeout_seconds": 1800,
                "workspace_source": "param",
                "cwd": str(tmp_path),
                "paths": paths,
                "paths_matched": paths_matched,
            },
            "context_summary": None,
        }
    )

    assert rebuilt.paths is None
    assert rebuilt.paths_matched is None


async def test_job_result_and_consume_are_bounded_end_to_end(monkeypatch, git_repo, tmp_path):
    """The retrieval TOOLS are bounded, not merely the helper they call.

    `_build_meta` being correct proves nothing about the routes that reach it, so
    this runs a real async job, plants over-cap selectors in its on-disk record --
    which is exactly the shape a pre-cap release left behind -- and reads it back
    through both public retrieval tools."""
    import json as _json
    import time as _time

    import anyio

    from claude_in_codex import claude as claude_mod
    from claude_in_codex import jobs

    monkeypatch.setenv("CLAUDE_IN_CODEX_STATE_DIR", str(tmp_path / "state"))
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": _json.dumps(
                {
                    "summary": "s",
                    "verdict": "concerns",
                    "confidence": "high",
                    "findings": [],
                    "questions": [],
                    "assumptions": [],
                }
            ),
            "total_cost_usd": 0.02,
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
    )
    monkeypatch.setattr(
        claude_mod,
        "build_command",
        lambda *a, **k: (["sh", "-c", "printf '%s' \"$0\"", envelope], []),
    )

    async with Client(mcp) as client:
        started = structured(
            await client.call_tool(
                "claude_review_changes_async",
                {"scope": "working_tree", "workspace_root": str(git_repo)},
            )
        )
        job_id = started["job_id"]
        deadline = _time.time() + 10
        while _time.time() < deadline:
            st = structured(
                await client.call_tool(
                    "claude_job_status", {"job_id": job_id, "workspace_root": str(git_repo)}
                )
            )
            if st["status"] != "running":
                break
            await anyio.sleep(0.05)
        assert st["status"] == "done"

        record_file = jobs._job_dir(str(git_repo), job_id) / "meta.json"
        record = _json.loads(record_file.read_text())
        record["extra"]["config"]["base"] = "b" * (MAX_REF_BYTES + 1)
        record["extra"]["config"]["scope"] = "branch"
        record["extra"]["config"]["paths"] = ["src/" + "a" * MAX_PATH_ENTRY_BYTES]
        record["extra"]["config"]["paths_matched"] = [7] * 50_000
        record_file.write_text(_json.dumps(record))

        for tool in ("claude_job_result", "claude_job_consume_result"):
            data = structured(
                await client.call_tool(
                    tool, {"job_id": job_id, "workspace_root": str(git_repo)}, raise_on_error=False
                )
            )
            # Retrieval SUCCEEDS -- the result was paid for -- but bounded.
            assert data["ok"] is True, data.get("error")
            assert data["meta"].get("paths") is None
            assert data["meta"].get("paths_matched") is None
            assert data["meta"].get("base") is None
            assert data["meta"].get("diff_range") is None
            assert _size(data) < ENVELOPE_CEILING_BYTES
            assert any("selector size caps" in w for w in data["meta"].get("security_warnings", []))
