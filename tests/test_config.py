import cc_plugin_codex.config as cfg


def test_inherit_flags():
    f = cfg.config_mode_flags("inherit")
    assert "--no-session-persistence" in f
    assert "--strict-mcp-config" in f
    assert '{"mcpServers":{}}' in f


def test_scoped_flags():
    f = cfg.config_mode_flags("scoped")
    assert "--setting-sources" in f and "project" in f
    assert "--strict-mcp-config" in f
    assert '{"mcpServers":{}}' in f


def test_bare_flags():
    f = cfg.config_mode_flags("bare")
    assert "--bare" in f
    assert "--no-session-persistence" in f
    assert "--strict-mcp-config" in f


def test_access_flags():
    assert cfg.access_flags("toolless") == ["--tools", ""]
    ro = cfg.access_flags("readonly")
    assert ro[:2] == ["--tools", "Read,Grep,Glob"]
    assert "Bash" in ro[-1]


def test_bare_available(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cfg.bare_available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert cfg.bare_available() is True


def test_defaults_from_env(monkeypatch):
    monkeypatch.setenv("CC_PLUGIN_CODEX_CLAUDE_CONFIG", "scoped")
    monkeypatch.setenv("CC_PLUGIN_CODEX_TIMEOUT_SECONDS", "240")
    d = cfg.defaults()
    assert d.config_mode == "scoped"
    assert d.timeout_seconds == 240
    assert d.access == "toolless"


def test_clamps():
    assert cfg.clamp_budget(99.0) == cfg.MAX_BUDGET_USD
    assert cfg.clamp_budget(0.0) == cfg.MIN_BUDGET_USD
    assert cfg.clamp_timeout(99999) == cfg.MAX_TIMEOUT_SECONDS
    assert cfg.clamp_timeout(1) == cfg.MIN_TIMEOUT_SECONDS


def test_critic_prompt_mentions_independence():
    assert "independent critique" in cfg.INDEPENDENT_CRITIC_PROMPT


def test_defaults_malformed_numeric_env_falls_back(monkeypatch):
    monkeypatch.setenv("CC_PLUGIN_CODEX_MAX_BUDGET_USD", "abc")
    monkeypatch.setenv("CC_PLUGIN_CODEX_TIMEOUT_SECONDS", "xyz")
    d = cfg.defaults()
    assert d.max_budget_usd == 1.00
    assert d.timeout_seconds == 180


def test_readonly_allowlist_has_no_write_tools():
    ro = cfg.access_flags("readonly")
    assert ro[1] == "Read,Grep,Glob"  # allowlist contains only read tools
    for bad in ("Edit", "Write", "NotebookEdit", "Bash"):
        assert bad not in ro[1]


def test_default_effort(monkeypatch):
    monkeypatch.delenv("CC_PLUGIN_CODEX_EFFORT", raising=False)
    assert cfg.defaults().effort == cfg.DEFAULT_EFFORT


def test_effort_from_env(monkeypatch):
    monkeypatch.setenv("CC_PLUGIN_CODEX_EFFORT", "xhigh")
    assert cfg.defaults().effort == "xhigh"


def test_sanitize_effort_falls_back_on_invalid():
    assert cfg.sanitize_effort("bogus") == cfg.DEFAULT_EFFORT
    assert cfg.sanitize_effort(None) == cfg.DEFAULT_EFFORT
    for level in cfg.VALID_EFFORTS:
        assert cfg.sanitize_effort(level) == level


def test_version_supported():
    assert cfg.version_supported("2.1.162 (Claude Code)") is True
    assert cfg.version_supported("3.0.0") is False
    assert cfg.version_supported("garbage") is None
    assert cfg.version_supported(None) is None


def test_supported_majors_env_override(monkeypatch):
    # A user can opt into an untested major without a code change.
    monkeypatch.setenv("CC_PLUGIN_CODEX_SUPPORTED_MAJORS", "2,3")
    assert cfg.supported_majors() == frozenset({2, 3})
    assert cfg.version_supported("3.0.0") is True
    assert cfg.version_supported("4.1.0") is False


def test_supported_majors_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("CC_PLUGIN_CODEX_SUPPORTED_MAJORS", "not,ints")
    assert cfg.supported_majors() == cfg.cli_contract.SUPPORTED_MAJORS
