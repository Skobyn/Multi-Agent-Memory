"""The monitor's own files (review 2026-11-19, idea B): a write to its ledger or config is flagged, never denied,
and a change to the merged config is reported to the user once."""
import json
import os

from helpers import make_entry
from test_hooks import _logged, _offline, _run

from epistemic_monitor import selfguard


def _hit(cfg, tool, **ti):
    return selfguard.protected_target(tool, ti, cfg)


def test_file_tools_flag_config_and_data_dir(cfg):
    assert _hit(cfg, "Write", file_path=str(cfg.cwd / ".claude" / "epistemic.json"), content="{}")
    assert _hit(cfg, "Write", file_path=".claude/epistemic.json", content="{}")            # relative
    assert _hit(cfg, "Edit", file_path=str(cfg.data_dir / "config.json"), new_string="x")
    assert _hit(cfg, "Write", file_path=str(cfg.data_dir / "ledger.jsonl"), content="x")
    assert not _hit(cfg, "Write", file_path=str(cfg.cwd / "src" / "app.py"), content="x")


def test_symlink_into_data_dir_flags(cfg, tmp_path):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    link = cfg.cwd / "innocent"
    os.symlink(cfg.data_dir, link)
    assert _hit(cfg, "Write", file_path=str(link / "config.json"), content="{}")


def test_settings_flag_only_when_hooks_or_env_change(cfg):
    p = str(cfg.cwd / ".claude" / "settings.json")
    assert _hit(cfg, "Write", file_path=p, content='{"disableAllHooks": true}')
    assert _hit(cfg, "Edit", file_path=p, new_string='"env": {"EPISTEMIC_GATE_ENFORCE": "false"}')
    assert not _hit(cfg, "Write", file_path=p, content='{"permissions": {"allow": ["Bash(ls)"]}}')


def test_bash_writes_flag(cfg):
    d = cfg.data_dir
    for cmd in (f"echo '{{}}' >> {d}/ledger.jsonl", f"rm {d}/index.db",
                f"sqlite3 {d}/index.db \"UPDATE entries SET status='retired'\"",
                "sed -i 's/true/false/' .claude/epistemic.json",
                "cd ~/.claude/projects/x/epistemic && rm index.db",
                "python3 -c 'import sqlite3' ~/.claude/projects/x/epistemic/index.db",
                "echo '{\"disableAllHooks\": true}' > .claude/settings.local.json"):
        assert _hit(cfg, "Bash", command=cmd), cmd


def test_reads_and_ledger_cli_do_not_flag(cfg):
    d = cfg.data_dir
    for cmd in (f"cat {d}/ledger.jsonl", f"grep D-0001 {d}/ledger.jsonl", "ls ~/.claude/projects/x/epistemic",
                "em-hook ledger status", "em-hook ledger confirm C-0001", "cat .claude/settings.json",
                "echo hello > notes.txt"):
        assert not _hit(cfg, "Bash", command=cmd), cmd
    assert _hit(cfg, "Bash", command=f"cat {d}/ledger.jsonl > /tmp/x; rm {d}/index.db")


def test_protected_write_is_advisory_not_deny(cfg, monkeypatch):
    _offline(monkeypatch)
    out = _run("pre_tool_use", {"tool_name": "Write", "tool_input": {
        "file_path": str(cfg.cwd / ".claude" / "epistemic.json"), "content": '{"gate": {"tools": []}}'}}, cfg)
    hso = out["hookSpecificOutput"]
    assert hso.get("permissionDecision") != "deny" and "monitor" in hso["additionalContext"]
    assert "writing" in out["systemMessage"]
    assert _logged(cfg, "monitor_tamper")[-1]["what"] == "protected_write"


def test_config_change_is_reported_once(cfg, monkeypatch, store):
    _offline(monkeypatch)
    make_entry(store, type="constraint", statement="Never edit vendor/")
    call = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    _run("pre_tool_use", call, cfg)                                 # baseline
    (cfg.cwd / ".claude").mkdir(exist_ok=True)
    (cfg.cwd / ".claude" / "epistemic.json").write_text(json.dumps({"gate": {"tools": [], "enforce": False}}))
    out = _run("pre_tool_use", call, cfg)
    assert "gate.enforce (true -> false)" in out["systemMessage"] and "gate.tools" in out["systemMessage"]
    rec = _logged(cfg, "monitor_tamper")[-1]
    assert rec["what"] == "config_changed" and any(k.startswith("gate.enforce") for k in rec["keys"])
    again = _run("pre_tool_use", call, cfg)
    assert not (again or {}).get("systemMessage")


def test_config_change_seen_at_session_start(cfg, monkeypatch):
    _offline(monkeypatch)
    _run("session_start", {"source": "startup"}, cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "config.json").write_text(json.dumps({"gate": {"deny_budget": 0}}))
    out = _run("session_start", {"source": "startup"}, cfg)
    assert "gate.deny_budget (5 -> 0)" in out["systemMessage"]


def test_deny_keeps_its_decision_with_notes():
    from epistemic_monitor.hooks import _with_notes
    from epistemic_monitor import hooklib
    d = _with_notes("pre_tool_use", hooklib.deny("pre_tool_use", "denied: C-0001"), "note", "user note")
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert d["hookSpecificOutput"]["permissionDecisionReason"].startswith("denied: C-0001")
    assert d["systemMessage"] == "user note"
