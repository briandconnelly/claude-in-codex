"""Build per-tool prompts and normalize claude's JSON envelope into the contract."""

from __future__ import annotations

import json
from typing import Any, cast

from cc_plugin_codex import cli_contract
from cc_plugin_codex.claude import ClaudeRun, classify_failure
from cc_plugin_codex.schemas import (
    Confidence,
    ContextSummary,
    ErrorInfo,
    ErrorResult,
    Finding,
    Meta,
    RawResponse,
    Severity,
    SuccessResult,
    Usage,
    Verdict,
)

_SCHEMA_INSTRUCTION = (
    "Respond with ONLY a single JSON object (no prose, no code fence) with keys: "
    '"summary" (string), "verdict" (one of pass|concerns|fail|unknown), '
    '"confidence" (one of low|medium|high), "findings" (array of objects with '
    "severity[critical|high|medium|low|nit], title, file, line, line_end (optional "
    "end line for multi-line findings), evidence, risk, recommendation), "
    '"questions" (array of strings), "assumptions" (array of strings), '
    '"next_steps" (array of strings: concrete actions to take next).'
)

_LEAD = {
    "claude_ask": "Give an independent second opinion on the following question.",
    "claude_review_changes": "Review the following code changes for correctness, "
    "regressions, security, and missing tests.",
    "claude_adversarial_review": "Attack the following plan/claim. Find the strongest "
    "counterarguments, failure modes, and risks.",
}

_VALID_VERDICT = {"pass", "concerns", "fail", "unknown"}
_VALID_CONFIDENCE = {"low", "medium", "high"}
_VALID_SEVERITY = {"critical", "high", "medium", "low", "nit"}


def _str_list(value: Any) -> list[str]:
    return [str(x) for x in value if x] if isinstance(value, list) else []


def build_prompt(tool: str, payload: dict[str, Any], context_text: str) -> str:
    parts = [_LEAD.get(tool, _LEAD["claude_ask"])]
    paths = payload.get("paths")
    paths_note = ""
    if paths:
        paths_note = (
            f" Path filter applied to the server-provided diff: {paths!r}. "
            "Treat findings as scoped to those paths; access=readonly may still "
            "allow direct workspace reads outside this filter."
        )
    if tool == "claude_ask":
        parts.append(payload["prompt"])
        if payload.get("context"):
            parts.append(f"\nAdditional context:\n{payload['context']}")
    elif tool == "claude_review_changes":
        if payload.get("focus"):
            parts.append(f"Focus especially on: {payload['focus']}.")
        parts.append(f"\nChanges (scope={payload.get('scope')}):{paths_note}\n{context_text}")
    elif tool == "claude_adversarial_review":
        parts.append(f"\nTarget:\n{payload['target']}")
        if payload.get("evidence"):
            parts.append(f"\nEvidence:\n{payload['evidence']}")
        if context_text:
            parts.append(f"\nRelated changes:{paths_note}\n{context_text}")
    parts.append("\n" + _SCHEMA_INSTRUCTION)
    return "\n".join(parts)


def extract_json(text: str) -> dict | None:
    decoder = json.JSONDecoder()

    def scan(candidate: str) -> dict | None:
        for idx, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    fence_start = 0
    while True:
        start = text.find("```", fence_start)
        if start < 0:
            break
        body_start = text.find("\n", start + 3)
        if body_start < 0:
            break
        end = text.find("```", body_start + 1)
        if end < 0:
            break
        parsed = scan(text[body_start + 1 : end])
        if parsed is not None:
            return parsed
        fence_start = end + 3

    return scan(text)


def _clamp(value: Any, allowed: set[str], default: str) -> str:
    return value if value in allowed else default


def _clean_findings(raw: Any) -> list[Finding]:
    findings: list[Finding] = []
    if not isinstance(raw, list):
        return findings
    for f in raw:
        if not isinstance(f, dict):
            continue
        if not all(f.get(k) for k in ("title", "evidence", "risk", "recommendation")):
            continue  # drop incomplete findings rather than fabricate fields
        line = f.get("line")
        line_end = f.get("line_end")
        findings.append(
            Finding(
                severity=cast("Severity", _clamp(f.get("severity"), _VALID_SEVERITY, "low")),
                title=str(f["title"]),
                file=str(f["file"]) if f.get("file") else None,
                line=line if isinstance(line, int) else None,
                line_end=line_end if isinstance(line_end, int) else None,
                evidence=str(f["evidence"]),
                risk=str(f["risk"]),
                recommendation=str(f["recommendation"]),
            )
        )
    return findings


def _error(info: ErrorInfo, meta: Meta) -> dict:
    return ErrorResult(error=info, meta=meta).model_dump(mode="json", exclude_none=True)


def apply_cost_usage(meta: Meta, env: dict) -> None:
    """Plumb total_cost_usd / usage from a claude JSON envelope onto meta.

    Used on both the success path and the non-zero-exit error path, so a failed
    paid call (e.g. budget_exceeded) still reports what it spent when available."""
    cost = env.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        meta.cost_usd = float(cost)
    raw_usage = env.get("usage")
    if isinstance(raw_usage, dict):
        meta.usage = Usage(
            input_tokens=raw_usage.get("input_tokens"),
            output_tokens=raw_usage.get("output_tokens"),
            cache_read_input_tokens=raw_usage.get("cache_read_input_tokens"),
            cache_creation_input_tokens=raw_usage.get("cache_creation_input_tokens"),
        )


def normalize_envelope(
    tool: str,
    stdout: str,
    meta: Meta,
    detail: str,
    context_summary: ContextSummary | None = None,
) -> dict:
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return _error(
            ErrorInfo(
                code="invalid_json",
                message="claude did not return valid JSON.",
                repair="Retry; if it persists, reduce context size.",
            ),
            meta,
        )

    if not isinstance(env, dict):
        return _error(
            ErrorInfo(
                code="invalid_json",
                message="claude did not return a JSON object.",
                repair="Retry; if it persists, update Claude Code or reduce context size.",
            ),
            meta,
        )

    # Plumb cost and usage onto meta regardless of success/error path.
    apply_cost_usage(meta, env)

    if env.get("is_error") or env.get("subtype") not in cli_contract.SUCCESS_SUBTYPES:
        return _error(
            classify_failure(
                ClaudeRun(
                    stdout=stdout,
                    stderr="",
                    exit_code=0,
                    elapsed_ms=meta.elapsed_ms,
                    timed_out=False,
                )
            ),
            meta,
        )

    text = env.get("result", "") or ""
    raw = RawResponse(
        text=text if detail == "full" else None,
        session_id=env.get("session_id"),
        model=next(iter(env.get("modelUsage") or {}), None),
    )
    inner = extract_json(text)

    # If Claude was blocked by denied tools AND produced nothing usable, surface it.
    denials = env.get("permission_denials") or []
    if denials and (inner is None and not text.strip()):
        return _error(
            ErrorInfo(
                code="claude_permission_error",
                message=f"claude was denied required tools: {str(denials)[:160]}",
                repair="Use access=toolless, or allow the needed read-only tools.",
            ),
            meta,
        )

    if inner is None:
        result = SuccessResult(
            tool=tool,
            summary=text.strip()[:500] or "(no content)",
            verdict="unknown",
            confidence="low",
            raw_response=raw,
            context_summary=context_summary if detail == "full" else None,
            meta=meta,
        )
        if denials:
            result.meta.permission_denials = denials
        return result.model_dump(mode="json", exclude_none=True)

    result = SuccessResult(
        tool=tool,
        summary=str(inner.get("summary", "")),
        verdict=cast("Verdict", _clamp(inner.get("verdict"), _VALID_VERDICT, "unknown")),
        confidence=cast("Confidence", _clamp(inner.get("confidence"), _VALID_CONFIDENCE, "low")),
        findings=_clean_findings(inner.get("findings", [])),
        questions=_str_list(inner.get("questions")),
        assumptions=_str_list(inner.get("assumptions")),
        next_steps=_str_list(inner.get("next_steps")),
        raw_response=raw,
        context_summary=context_summary if detail == "full" else None,
        meta=meta,
    )
    if denials:
        result.meta.permission_denials = denials
    return result.model_dump(mode="json", exclude_none=True)
