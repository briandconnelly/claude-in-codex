import json
from pathlib import Path

from claude_in_codex import cli_contract, jobs

ROOT = Path(__file__).resolve().parents[1]
MCP_CONFIGS = (ROOT / ".mcp.json", ROOT / "plugins" / "claude-in-codex" / ".mcp.json")

EXPECTED_ENV_VARS = {
    "ANTHROPIC_API_KEY",
    "CLAUDE_IN_CODEX_ACCESS",
    "CLAUDE_IN_CODEX_CLAUDE_CONFIG",
    "CLAUDE_IN_CODEX_EFFORT",
    "CLAUDE_IN_CODEX_GIT_TIMEOUT_SECONDS",
    jobs.MAX_COUNT_ENV,
    jobs.MAX_SECONDS_ENV,
    jobs.TTL_ENV,
    "CLAUDE_IN_CODEX_MAX_BUDGET_USD",
    "CLAUDE_IN_CODEX_MAX_INPUT_BYTES",
    "CLAUDE_IN_CODEX_MODEL",
    jobs.STATE_ENV,
    cli_contract.SUPPORTED_MAJORS_ENV,
    "CLAUDE_IN_CODEX_TIMEOUT_SECONDS",
}


def _env_vars(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    return data["mcpServers"]["claude-in-codex"]["env_vars"]


def test_bundled_mcp_configs_forward_supported_env_vars():
    all_env_vars = [_env_vars(path) for path in MCP_CONFIGS]
    assert all(env_vars == all_env_vars[0] for env_vars in all_env_vars)
    assert len(all_env_vars[0]) == len(set(all_env_vars[0]))
    assert set(all_env_vars[0]) == EXPECTED_ENV_VARS
