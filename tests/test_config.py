import json

from epistemic_monitor.config import DEFAULTS, load_config, project_slug


def test_slug_matches_claude_code():
    assert project_slug("/home/user/Multi-Agent-Memory") == "-home-user-Multi-Agent-Memory"


def test_defaults_and_paths(tmp_path):
    env = {"EPISTEMIC_DATA_DIR": str(tmp_path / "d")}
    cfg = load_config(cwd=tmp_path, environ=env)
    assert cfg.get("decision_model", "provider") == "typesafe"
    assert cfg.get("advisor", "model") == "claude-opus-5-5"
    assert cfg.get("extractor", "model") == "claude-haiku-5-5"
    assert cfg.get("gate", "enforce") is True  # Phase 2: enforcement on by default, budgeted
    assert cfg.ledger_path == tmp_path / "d" / "ledger.jsonl"
    assert cfg.memory_md_path.name == "MEMORY.md"


def test_file_and_env_precedence(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "epistemic.json").write_text(json.dumps({"gate": {"deny_budget": 9}, "advisor": {"model": "x"}}))
    d = tmp_path / "d"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"advisor": {"model": "from-data-dir"}}))
    env = {"EPISTEMIC_DATA_DIR": str(d), "EPISTEMIC_GATE_ENFORCE": "true", "EPISTEMIC_DECISION_MODEL_MAX_RPS": "5"}
    cfg = load_config(cwd=tmp_path, environ=env)
    assert cfg.get("gate", "deny_budget") == 9
    assert cfg.get("advisor", "model") == "from-data-dir"
    assert cfg.get("gate", "enforce") is True
    assert cfg.get("decision_model", "max_rps") == 5
    assert cfg.get("decision_model", "model") == DEFAULTS["decision_model"]["model"]


def test_plugin_root_config_applies_everywhere_but_yields_to_project_and_env(tmp_path, monkeypatch):
    from epistemic_monitor.config import load_config, plugin_root
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"advisor": {"provider": "claude-cli", "model": "claude-opus-5-5"},
                                                   "gate": {"deny_budget": 3}}))
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    env = {"CLAUDE_PLUGIN_ROOT": str(root), "EPISTEMIC_DATA_DIR": str(tmp_path / "data")}
    assert plugin_root(env) == root
    cfg = load_config(cwd=proj, environ=env)
    assert cfg.get("advisor", "provider") == "claude-cli" and cfg.get("gate", "deny_budget") == 3
    (proj / ".claude" / "epistemic.json").write_text(json.dumps({"gate": {"deny_budget": 4}}))
    cfg = load_config(cwd=proj, environ={**env, "EPISTEMIC_ADVISOR_MODEL": "claude-sonnet-5-5"})
    assert cfg.get("gate", "deny_budget") == 4 and cfg.get("advisor", "model") == "claude-sonnet-5-5"
    assert cfg.get("advisor", "provider") == "claude-cli"
    # without CLAUDE_PLUGIN_ROOT the checkout itself is the plugin root
    assert (plugin_root({}) / "hooks" / "hooks.json").exists()


def test_user_level_epistemic_json_sits_between_plugin_and_project(tmp_path):
    from epistemic_monitor.config import load_config
    root, home, proj = tmp_path / "plugin", tmp_path / "home", tmp_path / "proj"
    root.mkdir(); home.mkdir(); (proj / ".claude").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"advisor": {"provider": "openrouter"}, "gate": {"deny_budget": 2}}))
    (home / "epistemic.json").write_text(json.dumps({"advisor": {"provider": "claude-cli"}, "embeddings": {"provider": "none"}}))
    env = {"CLAUDE_PLUGIN_ROOT": str(root), "CLAUDE_CONFIG_DIR": str(home), "EPISTEMIC_DATA_DIR": str(tmp_path / "data")}
    cfg = load_config(cwd=proj, environ=env)
    assert cfg.get("advisor", "provider") == "claude-cli" and cfg.get("embeddings", "provider") == "none"
    assert cfg.get("gate", "deny_budget") == 2
    (proj / ".claude" / "epistemic.json").write_text(json.dumps({"advisor": {"provider": "anthropic"}}))
    assert load_config(cwd=proj, environ=env).get("advisor", "provider") == "anthropic"
