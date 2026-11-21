"""Advisory flag on writes into the monitor's own data directory (docs/RESEARCH-2026-11-21.md, idea C shrunk)."""
import io
import json

import pytest
from fakes import FakeDM, FakeLLM
from helpers import make_entry

from epistemic_monitor import datadir, hooklib, hooks
from epistemic_monitor.hooks import HANDLERS
from epistemic_monitor.store import LedgerStore


def _run(payload, cfg):
    out = io.StringIO()
    payload = {"session_id": "s1", "cwd": str(cfg.cwd), "hook_event_name": "PreToolUse", **payload}
    assert hooklib.run("pre_tool_use", HANDLERS["pre_tool_use"], payload=payload, stdout=out) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def _flagged(cfg):
    s = LedgerStore(cfg)
    try:
        return [r for r in s.recent_interventions(100) if r.get("kind") == "data_dir_write"]
    finally:
        s.close()


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(hooks, "make_llm", lambda section: FakeLLM([]))
    monkeypatch.setattr(hooks, "make_decision_model", lambda cfg, fb=None: FakeDM(default=0.05))
    monkeypatch.setenv("EPISTEMIC_EMBEDDINGS_PROVIDER", "none")


def _bash_cases(cfg):
    L = cfg.ledger_path
    return [
        f"sed -i 's/\"source\": \"agent\"/\"source\": \"user\"/' {L}",
        f"python3 -c \"open('{L}','a').write('x')\"",
        "python3 -c \"from epistemic_monitor.store import LedgerStore; LedgerStore(c).confirm('D-0001', by='user')\"",
        f"python3 -c \"import pathlib; pathlib.Path('{L}').write_text('')\"",
        "sqlite3 ~/.claude/projects/x/epistemic/index.db \"update entries set json='{}'\"",
        f"rm {cfg.data_dir}/pinned.md",
        f"cat {L} > /tmp/x && cp /tmp/x {L}",
    ]


def test_bash_writes_into_the_data_dir_get_an_advisory_note(cfg, store, offline):
    store.close()
    for cmd in _bash_cases(cfg):
        out = _run({"tool_name": "Bash", "tool_input": {"command": cmd}}, cfg)
        assert out is not None, cmd
        hso = out["hookSpecificOutput"]
        assert "permissionDecision" not in hso, cmd               # advisory, never a deny
        assert "own ledger data" in hso["additionalContext"], cmd
    assert len(_flagged(cfg)) == len(_bash_cases(cfg))


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_path_tools_into_the_data_dir_are_flagged(cfg, store, offline, tool, key):
    store.close()
    for target in (cfg.ledger_path, cfg.index_path):
        out = _run({"tool_name": tool, "tool_input": {key: str(target), "content": "x"}}, cfg)
        assert "own ledger data" in out["hookSpecificOutput"]["additionalContext"]


def test_reads_and_project_edits_are_not_flagged(cfg, store, offline):
    store.close()
    quiet = [("Bash", {"command": f"tail -n 5 {cfg.ledger_path} | jq .event"}),
             ("Bash", {"command": f"grep -c supersede {cfg.ledger_path} 2>/dev/null"}),
             ("Bash", {"command": "grep -rn LedgerStore src/"}),
             ("Bash", {"command": "sed -i 's/a/b/' src/epistemic_monitor/store.py"}),
             ("Edit", {"file_path": str(cfg.cwd / "docs" / "ledger.md"), "old_string": "a", "new_string": "b"}),
             ("Write", {"file_path": "src/ledger_index.py", "content": "x"})]
    for tool, ti in quiet:
        assert _run({"tool_name": tool, "tool_input": ti}, cfg) is None, ti
    assert _flagged(cfg) == []


def test_read_only_parser_errs_toward_flagging(cfg):
    L = cfg.ledger_path
    assert datadir.touched("Bash", {"command": f"cat {L} | tee {L}.bak"}, cfg)          # tee is not read-only
    assert datadir.touched("Bash", {"command": f"cat $(echo {L})"}, cfg)                 # substitution
    assert datadir.touched("Bash", {"command": f"head {L} >> {L}"}, cfg)                # redirect into a file
    assert datadir.touched("Bash", {"command": f"cat {L} 2>&1"}, cfg) is None


def test_note_joins_a_gate_advisory_and_does_not_soften_a_deny(cfg, store, offline, monkeypatch):
    make_entry(store, statement="Never write FORBIDDEN_MARK anywhere.",
               falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"FORBIDDEN_MARK"}])
    store.close()
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(cfg.ledger_path), "content": "FORBIDDEN_MARK"}}
    monkeypatch.setenv("EPISTEMIC_GATE_ENFORCE", "false")
    ctx = _run(payload, cfg)["hookSpecificOutput"]["additionalContext"]
    assert "D-0001" in ctx and "own ledger data" in ctx
    monkeypatch.setenv("EPISTEMIC_GATE_ENFORCE", "true")
    out = _run(payload, cfg)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert [r["gate_decision"] for r in _flagged(cfg)] == ["advisory", "deny"]


def test_flag_can_be_turned_off(cfg, store, offline, monkeypatch):
    store.close()
    monkeypatch.setenv("EPISTEMIC_GATE_DATA_DIR_FLAG", "false")
    assert _run({"tool_name": "Bash", "tool_input": {"command": f"rm {cfg.ledger_path}"}}, cfg) is None


def test_second_round_false_negatives(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))          # data_dir is tmp_path/epistemic, so ~/epistemic names it
    D = cfg.data_dir
    for cmd in ("rm -rf ~/epistemic", "cd $HOME/epistemic && rm *", "rm ~/.claude/projects/*/epist*/*",
                f"cat x & rm {D}/ledger.jsonl", f"sort -o {D}/ledger.jsonl {D}/ledger.jsonl",
                f"sed -ni 's/a/b/' {D}/ledger.jsonl", f"find {D} -name '*.db' -delete",
                f"git checkout -- {D}/ledger.jsonl"):
        assert datadir.touched("Bash", {"command": cmd}, cfg), cmd


def test_second_round_false_positives(cfg):
    D = cfg.data_dir
    for cmd in (f"grep -i foo {D}/ledger.jsonl", f"sed -n 1,5p {D}/ledger.jsonl", f"find {D} -name '*.jsonl'",
                f"cat {D}/ledger.jsonl >>/dev/null", f"cat {D}/ledger.jsonl 2>&1 | tail -3",
                "sqlite3 index.db 'insert into t values (1)'", "touch pinned.md", "mkdir -p docs/epistemic/notes",
                "git commit -m 'store: replay ledger.jsonl via LedgerStore'", "python3 -m pytest -k LedgerStore",
                f"git log -- {D}"):
        assert datadir.touched("Bash", {"command": cmd}, cfg) is None, cmd


def test_multiedit_is_flagged(cfg):
    assert datadir.touched("MultiEdit", {"file_path": str(cfg.ledger_path), "edits": []}, cfg)


def test_note_survives_an_authority_advisory(cfg, store, offline, monkeypatch):
    make_entry(store)
    store.close()
    monkeypatch.setenv("EPISTEMIC_GATE_ENFORCE", "false")
    cmd = f'em-hook ledger confirm D-0001 && sed -i "s/a/b/" {cfg.ledger_path}'
    out = _run({"tool_name": "Bash", "tool_input": {"command": cmd}}, cfg)["hookSpecificOutput"]
    assert out["additionalContext"].startswith("Epistemic monitor note: this call writes into")
    assert [r["gate_decision"] for r in _flagged(cfg)] == ["advisory"]
