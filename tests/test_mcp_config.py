import json
from pathlib import Path

from cc_plugin_codex import cli_contract, jobs

ROOT = Path(__file__).resolve().parents[1]
MCP_CONFIGS = (ROOT / ".mcp.json", ROOT / "plugins" / "cc-plugin-codex" / ".mcp.json")

EXPECTED_ENV_VARS = {
    "ANTHROPIC_API_KEY",
    "CC_PLUGIN_CODEX_ACCESS",
    "CC_PLUGIN_CODEX_CLAUDE_CONFIG",
    "CC_PLUGIN_CODEX_EFFORT",
    "CC_PLUGIN_CODEX_GIT_TIMEOUT_SECONDS",
    jobs.MAX_COUNT_ENV,
    jobs.MAX_SECONDS_ENV,
    jobs.TTL_ENV,
    "CC_PLUGIN_CODEX_MAX_BUDGET_USD",
    "CC_PLUGIN_CODEX_MAX_INPUT_BYTES",
    "CC_PLUGIN_CODEX_MODEL",
    jobs.STATE_ENV,
    cli_contract.SUPPORTED_MAJORS_ENV,
    "CC_PLUGIN_CODEX_TIMEOUT_SECONDS",
}


def _env_vars(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    return data["mcpServers"]["cc-plugin-codex"]["env_vars"]


def test_bundled_mcp_configs_forward_supported_env_vars():
    all_env_vars = [_env_vars(path) for path in MCP_CONFIGS]
    assert all(env_vars == all_env_vars[0] for env_vars in all_env_vars)
    assert set(all_env_vars[0]) == EXPECTED_ENV_VARS
