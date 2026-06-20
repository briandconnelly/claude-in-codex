import claude_in_codex.config as cfg


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


def test_safe_flags():
    f = cfg.config_mode_flags("safe")
    assert "--safe-mode" in f
    assert "--no-session-persistence" in f
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


def test_is_env_placeholder():
    assert cfg.is_env_placeholder("${ANTHROPIC_API_KEY}") is True
    assert cfg.is_env_placeholder("${VAR_1}") is True
    assert cfg.is_env_placeholder("  ${VAR}  ") is True  # tolerates surrounding whitespace
    assert cfg.is_env_placeholder("sk-real-key") is False
    assert cfg.is_env_placeholder("prefix${VAR}") is False  # not a whole-value placeholder
    assert cfg.is_env_placeholder("${HOME}/state") is False  # embedded, deliberately not flagged
    assert cfg.is_env_placeholder("") is False
    assert cfg.is_env_placeholder(None) is False
    # Malformed ${...} forms are not valid shell var names -> not a substitution failure.
    assert cfg.is_env_placeholder("${}") is False
    assert cfg.is_env_placeholder("${ VAR }") is False
    assert cfg.is_env_placeholder("${1ABC}") is False


def test_placeholder_env_vars_scans_tracked_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "${ANTHROPIC_API_KEY}")
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "${CLAUDE_IN_CODEX_CLAUDE_CONFIG}")
    monkeypatch.setenv("CLAUDE_IN_CODEX_ACCESS", "readonly")  # real value, not flagged
    monkeypatch.setenv("UNRELATED_VAR", "${UNRELATED_VAR}")  # not tracked
    assert cfg.placeholder_env_vars() == [
        "ANTHROPIC_API_KEY",
        "CLAUDE_IN_CODEX_CLAUDE_CONFIG",
    ]


def test_placeholder_env_vars_empty_when_expanded(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "scoped")
    assert cfg.placeholder_env_vars() == []


def test_defaults_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_CLAUDE_CONFIG", "scoped")
    monkeypatch.setenv("CLAUDE_IN_CODEX_TIMEOUT_SECONDS", "240")
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
    assert "untrusted DATA" in cfg.INDEPENDENT_CRITIC_PROMPT
    assert "credentials or secrets" in cfg.INDEPENDENT_CRITIC_PROMPT


def test_workspace_hook_settings_detects_hooks(tmp_path):
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text('{"hooks":{"SessionStart":[]}}')
    assert cfg.workspace_hook_settings(str(tmp_path)) == [".claude/settings.json"]


def test_workspace_hook_settings_ignores_undecodable_file(tmp_path):
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    # Non-UTF8 / binary content must not crash the advisory scan.
    (settings_dir / "settings.json").write_bytes(b"\xff\xfe\x00\x01")
    assert cfg.workspace_hook_settings(str(tmp_path)) == []


def test_hook_security_warnings_skip_bare(tmp_path):
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.local.json").write_text('{"hooks":{"SessionStart":[]}}')
    assert cfg.hook_security_warnings(str(tmp_path), "inherit")
    assert cfg.hook_security_warnings(str(tmp_path), "safe") == []
    assert cfg.hook_security_warnings(str(tmp_path), "bare") == []


def test_hooks_disabled_available_requires_api_key_for_bare(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cfg.hooks_disabled_available("safe") is True
    assert cfg.hooks_disabled_available("safe", True, frozenset()) is False
    assert cfg.safe_available(True, frozenset({"--safe-mode"})) is True
    assert cfg.hooks_disabled_available("bare") is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert cfg.hooks_disabled_available("bare") is True


def test_defaults_malformed_numeric_env_falls_back(monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_MAX_BUDGET_USD", "abc")
    monkeypatch.setenv("CLAUDE_IN_CODEX_TIMEOUT_SECONDS", "xyz")
    d = cfg.defaults()
    assert d.max_budget_usd == 1.00
    assert d.timeout_seconds == 180


def test_readonly_allowlist_has_no_write_tools():
    ro = cfg.access_flags("readonly")
    assert ro[1] == "Read,Grep,Glob"  # allowlist contains only read tools
    for bad in ("Edit", "Write", "NotebookEdit", "Bash"):
        assert bad not in ro[1]


def test_default_effort(monkeypatch):
    monkeypatch.delenv("CLAUDE_IN_CODEX_EFFORT", raising=False)
    assert cfg.defaults().effort == cfg.DEFAULT_EFFORT


def test_effort_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_EFFORT", "xhigh")
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
    monkeypatch.setenv("CLAUDE_IN_CODEX_SUPPORTED_MAJORS", "2,3")
    assert cfg.supported_majors() == frozenset({2, 3})
    assert cfg.version_supported("3.0.0") is True
    assert cfg.version_supported("4.1.0") is False


def test_supported_majors_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("CLAUDE_IN_CODEX_SUPPORTED_MAJORS", "not,ints")
    assert cfg.supported_majors() == cfg.cli_contract.SUPPORTED_MAJORS
