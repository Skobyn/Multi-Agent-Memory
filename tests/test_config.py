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
