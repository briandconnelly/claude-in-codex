"""ClaudeBackend: this bridge's adapter on the pontonier AgentBackend protocol.

A faithful thin layer over the proven functions in `claude.py` — command
construction, environment scrubbing, and classification delegate to the same
code the tool paths run, so the adapter cannot drift from production behavior.
The protocol it fits is frozen (contract_api_version = 1), and the tools are
re-plumbed through it: every model-bearing run stages via `prepare()`, so the
adapter cannot drift from production behavior — it is production behavior.

Protocol-fit notes, discovered here and carried into the frozen line:

* Classification is config_mode-aware end to end (`classify_failure(run,
  config_mode=...)` picks auth repairs per mode, and env scrubbing branches on
  bare). `RunRequest.config_mode` carries it, but the shared classifier's
  backend hook receives only (outcome, request) — sufficient here, worth
  documenting as the pattern for mode-dependent backends.
* This backend has no separate answer artifact at all: answer, cost, and
  session id all live in the stdout JSON envelope. `finalize` therefore reads
  `outcome.run.stdout` exclusively — the protocol's artifact_texts channel goes
  unused, which is fine, but the docs should say a backend may use neither
  events nor artifacts.
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import TYPE_CHECKING

from pontonier.backend.protocol import ClassifiedFailure, ExecResult, PreparedRun, Usage

from claude_in_codex import claude, cli_contract, config, normalize, preflight
from claude_in_codex.cli_contract import PONTONIER_CONTRACT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pontonier.backend.protocol import RunOutcome, RunRequest

CONTRACT = PONTONIER_CONTRACT

_KIND_DEFAULT_ACCESS = "toolless"


class ClaudeBackend:
    """The behavior half of the Claude contract (facts live on PONTONIER_CONTRACT)."""

    def validate_request(self, request: RunRequest) -> ClassifiedFailure | None:
        # Upstream rejects a bad --effort at arg-parse (loud, zero spend), and this
        # server also enforces VALID_EFFORTS at its boundary; mirror that boundary
        # here so a direct adapter caller cannot send a value the tools would refuse.
        effort = request.reasoning_effort
        if effort is not None and effort not in cli_contract.VALID_EFFORTS:
            return ClassifiedFailure(
                code="invalid_reasoning_effort",
                detail=f"reasoning_effort must be one of {', '.join(cli_contract.VALID_EFFORTS)}.",
            )
        return None

    @contextlib.asynccontextmanager
    async def prepare(self, request: RunRequest) -> AsyncIterator[PreparedRun]:
        """Stage exactly what the tool paths stage: the shared command builder
        (config-mode flags, access flags, guardrail system prompt, budget), the
        per-mode scrubbed environment, and the prompt over stdin. No file
        artifacts: answer, cost, and session id all arrive in the stdout JSON
        envelope."""
        config_mode = request.config_mode or config.defaults().config_mode
        cmd, dropped = claude.build_command(
            request.prompt,
            config_mode,
            request.access or _KIND_DEFAULT_ACCESS,
            request.model,
            request.budget_usd
            if request.budget_usd is not None
            else config.defaults().max_budget_usd,
            effort=request.reasoning_effort,
            flag_support=preflight.flag_support(),
        )
        # This backend stages NO file artifacts, so nothing here outlives the
        # context — which is what lets the async job path use prepare() purely to
        # obtain argv/dropped_flags and hand them to the detached job store.
        yield PreparedRun(
            argv=tuple(cmd),
            env=self.scrub_env(dict(os.environ), config_mode),
            cwd=request.cwd,
            stdin_text=request.prompt,
            dropped_flags=tuple(dropped),
        )

    def finalize(self, outcome: RunOutcome, request: RunRequest) -> ExecResult:
        # Tolerant raw-envelope read (the keys in cli_contract.ENVELOPE_KEYS);
        # normalize.normalize_envelope renders the full tool contract and is
        # meta-coupled, so the adapter reads the same envelope one level down.
        try:
            envelope = json.loads(outcome.run.stdout)
        except json.JSONDecodeError:
            envelope = {}
        if not isinstance(envelope, dict):
            envelope = {}
        # `result` is declared a string, but this is the TOLERANT read and CLI
        # drift can put an object, a list, or a number here. Coerce before use:
        # extract_json runs string methods on it, and ExecResult.answer is typed
        # `str`, so a raw non-string would either raise or escape into the
        # protocol result.
        raw_answer = envelope.get("result")
        answer = raw_answer if isinstance(raw_answer, str) else ""
        structured = (
            normalize.extract_json(answer) if request.schema is not None and answer else None
        )
        # CLI drift can put anything here. This is the TOLERANT read, so a
        # non-object usage block is ignored rather than raised on — the same
        # guard normalize.normalize_envelope applies one level up. A reported
        # cost still survives a malformed usage block.
        raw_usage = envelope.get("usage")
        usage_blob = raw_usage if isinstance(raw_usage, dict) else {}
        cost = envelope.get("total_cost_usd")
        usage = None
        if usage_blob or cost is not None:
            usage = Usage(
                input_tokens=usage_blob.get("input_tokens"),
                output_tokens=usage_blob.get("output_tokens"),
                cost_usd=cost,
            )
        return ExecResult(
            answer=answer,
            structured=structured,
            usage=usage,
            session_id=envelope.get("session_id"),
        )

    def classify_failure(self, outcome: RunOutcome, request: RunRequest) -> ClassifiedFailure:
        info = claude.classify_failure(
            claude.ClaudeRun(
                stdout=outcome.run.stdout,
                stderr=outcome.run.stderr,
                exit_code=outcome.run.exit_code,
                elapsed_ms=outcome.run.elapsed_ms,
                timed_out=outcome.run.timed_out,
            ),
            config_mode=request.config_mode or config.defaults().config_mode,
        )
        return ClassifiedFailure(
            code=info.code,
            detail=info.message,
            retry_after_ms=info.retry_after_ms,
        )

    def list_models(self) -> tuple[str, ...]:
        return tuple(slug for slug, _name, _kind in cli_contract.KNOWN_MODELS)

    def auth_probe(self) -> bool | None:
        authenticated, _detail = claude.auth_status(config_mode=config.defaults().config_mode)
        return authenticated

    def scrub_env(self, env: dict[str, str], config_mode: str | None) -> dict[str, str]:
        # Scrub the env we were GIVEN. Login-backed modes (inherit/scoped/safe)
        # must use Claude Code's OAuth/session path, so a stale
        # ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN cannot be allowed to override
        # it; bare mode NEEDS the key and keeps it. This mirrors
        # claude._claude_subprocess_env's policy without adopting its return
        # value, which is a full os.environ copy and would discard the argument.
        if config_mode not in claude._LOGIN_MODES:
            return dict(env)
        return {k: v for k, v in env.items() if k not in claude._LOGIN_CREDENTIAL_ENV_VARS}


# The adapter is stateless; every production path shares this instance.
BACKEND = ClaudeBackend()


# Explicit, not substring matching: `"review" in tool` split claude_dry_run from
# its own deprecated alias claude_review_dry_run. request.kind is inert today,
# but an alias must never disagree with its canonical name.
_TOOL_KINDS = {
    "claude_review_changes": "review_changes",
    "claude_review_changes_async": "review_changes",
    "claude_adversarial_review": "review_changes",
}


def kind_for_tool(tool: str) -> str:
    """Map this bridge's tool names onto the protocol's canonical verbs.

    `claude_adversarial_review` is a backend-specific extension, but it is still
    a review of gathered changes — same access posture, same envelope."""
    return _TOOL_KINDS.get(tool, "consult")
