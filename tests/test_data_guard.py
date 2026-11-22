"""The agent cannot take a user entry out of enforcement by writing the plugin's own data (RESEARCH-2026-11-22, idea T).

Probes (review of the idea): ``sqlite3 index.db "update entries set status='retired'"``, an Edit of ledger.jsonl, and
a forged ``status`` event followed by ``rm index.db`` all stopped the gate enforcing a user decision, with no record.
"""

import json
import sqlite3

from fakes import FakeDM
from helpers import make_entry
from test_hooks import _logged, _offline, _run

from epistemic_monitor import data_guard
from epistemic_monitor.store import LedgerStore

PAY = {"tool_name": "Edit", "tool_use_id": "t1",
       "tool_input": {"file_path": "src/db/client.py", "old_string": "a", "new_string": "import pymongo"}}


def _decision(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")


def _setup(cfg, store, monkeypatch):
    _offline(monkeypatch, dm=FakeDM(default=0.05))
    make_entry(store, scope={"paths": ["src/db/*"]},
               falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"\bpymongo\b"}])
    store.close()
    assert _decision(_run("pre_tool_use", PAY, cfg)) == "deny"


def _bash(cfg, command):
    return _run("pre_tool_use", {"tool_name": "Bash", "tool_use_id": "b1", "tool_input": {"command": command}}, cfg)


# ------------------------------------------------------------------ the guard, end to end through pre_tool_use

def test_sqlite_update_of_index_is_denied_and_logged(cfg, store, monkeypatch):
    _setup(cfg, store, monkeypatch)
    out = _bash(cfg, f"sqlite3 {cfg.index_path} \"update entries set status='retired' where id='D-0001'\"")
    assert _decision(out) == "deny"
    assert "ledger" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert _logged(cfg, "data_dir_guard")[-1]["decision"] == "deny"


def test_edit_of_ledger_jsonl_is_denied(cfg, store, monkeypatch):
    _setup(cfg, store, monkeypatch)
    out = _run("pre_tool_use", {"tool_name": "Edit", "tool_input": {
        "file_path": str(cfg.ledger_path), "old_string": '"active"', "new_string": '"retired"'}}, cfg)
    assert _decision(out) == "deny"


def test_guard_advisory_when_enforcement_off(cfg, store, monkeypatch):
    _setup(cfg, store, monkeypatch)
    monkeypatch.setenv("EPISTEMIC_GATE_ENFORCE", "false")
    out = _bash(cfg, f"rm {cfg.index_path}")
    assert _decision(out) is None
    assert "ledger" in json.dumps(out)


def test_guard_can_be_turned_off(cfg, store, monkeypatch):
    _offline(monkeypatch)
    store.close()
    monkeypatch.setenv("EPISTEMIC_GATE_DATA_DIR_GUARD", "false")
    assert _decision(_bash(cfg, f"rm {cfg.index_path}")) is None


# ------------------------------------------------------------------ the classifier

def _check(cfg, command):
    return data_guard.check("Bash", {"command": command}, cfg.data_dir, str(cfg.cwd))


def test_bash_writes_to_the_data_dir_are_caught(cfg):
    d = cfg.data_dir
    for cmd in [
        f"rm {d}/index.db",
        f"rm -f {d}/index.db*",
        f"cd {d} && rm index.db",
        f"echo '{{\"event\":\"status\",\"id\":\"D-0001\",\"status\":\"retired\",\"by\":\"user\"}}' >> {d}/ledger.jsonl",
        f"sed -i 's/active/retired/' {d}/ledger.jsonl",
        f"mv {d}/ledger.jsonl /tmp/x",
        f"sqlite3 {d}/index.db \"DELETE FROM entries\"",
        f"python3 -c \"import sqlite3; c=sqlite3.connect('{d}/index.db'); c.execute('update entries set status=1')\"",
        "python3 -c \"from epistemic_monitor.store import LedgerStore; s=LedgerStore(c); s.retire('D-0001','x',by='user')\"",
        "em-hook ledger confirm D-0001 && rm ~/.claude/projects/-home-x/epistemic/index.db",
        "cat new.jsonl | tee ~/.claude/projects/-home-x/epistemic/ledger.jsonl",
    ]:
        assert _check(cfg, cmd), cmd


def test_reads_and_unrelated_writes_are_allowed(cfg):
    d = cfg.data_dir
    for cmd in [
        f"cat {d}/pinned.md",
        f"sqlite3 {d}/index.db \"select id, status from entries\"",
        f"tail -n 50 {d}/interventions.jsonl | grep gate",
        f"grep D-0001 {d}/ledger.jsonl 2>&1",
        "python3 -I bin/em-hook ledger list",
        "em-hook ledger add --type constraint --statement 'Never edit vendor/' --user",
        "rm -rf build/ && echo done > out.txt",
        "sed -i 's/a/b/' src/x.py",
    ]:
        assert _check(cfg, cmd) is None, cmd


def test_edit_tools_inside_and_outside_the_data_dir(cfg):
    assert data_guard.check("Write", {"file_path": str(cfg.data_dir / "pinned.md")}, cfg.data_dir)
    assert data_guard.check("NotebookEdit", {"notebook_path": str(cfg.data_dir / "x.ipynb")}, cfg.data_dir)
    assert data_guard.check("Edit", {"file_path": str(cfg.cwd / "src/x.py")}, cfg.data_dir, str(cfg.cwd)) is None


# ------------------------------------------------------------------ the replay rule (forged lines in ledger.jsonl)

def _forge_and_rebuild(cfg, event):
    with open(cfg.ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
    for p in cfg.data_dir.glob("index.db*"):
        p.unlink()
    s = LedgerStore(cfg)
    try:
        return s.get_entry("D-0001")
    finally:
        s.close()


def test_forged_status_without_by_does_not_retire_a_user_entry(cfg, store):
    make_entry(store)
    store.close()
    e = _forge_and_rebuild(cfg, {"event": "status", "id": "D-0001", "status": "retired"})
    assert e.status == "active"
    assert _logged(cfg, "user_entry_write_refused")[-1]["replay"] is True


def test_forged_entry_update_cannot_rewrite_or_demote_a_user_entry(cfg, store):
    make_entry(store)
    store.close()
    e = _forge_and_rebuild(cfg, {"event": "entry_update", "id": "D-0001", "fields": {"statement": "Use MongoDB."}})
    assert e.statement == "Use PostgreSQL as the primary database."
    e = _forge_and_rebuild(cfg, {"event": "entry_update", "id": "D-0001",
                                 "fields": {"provenance": {"source": "agent", "confirmed_by_user": False}}})
    assert e.user_authority


def test_forged_re_add_cannot_overwrite_a_user_entry(cfg, store):
    e0 = make_entry(store)
    store.close()
    d = e0.to_dict() | {"status": "retired"}
    assert _forge_and_rebuild(cfg, {"event": "entry_add", "entry": d}).status == "active"


def test_user_retire_and_review_flows_still_work(cfg, store):
    make_entry(store)
    agent = make_entry(store, type="premise", statement="The cache is warm.", source="agent", confirmed=False)
    store.set_status(agent.id, "unknown_current")  # agent entries are not protected
    assert store.get_entry(agent.id).status == "unknown_current"
    assert store.retire("D-0001", "user changed their mind", by="user")
    assert store.get_entry("D-0001").status == "retired"
    store.close()
    for p in cfg.data_dir.glob("index.db*"):
        p.unlink()
    s = LedgerStore(cfg)
    try:
        assert s.get_entry("D-0001").status == "retired"  # replay keeps the user's own retirement
    finally:
        s.close()


def test_resolve_keep_on_a_user_review_item_is_allowed(cfg, store):
    e = make_entry(store)
    store._upsert(type(e).from_dict(e.to_dict() | {"status": "orphaned"}))
    assert store.resolve(e.id, keep=True)
    assert store.get_entry(e.id).status == "active"


def test_code_path_set_status_on_a_user_entry_is_refused(cfg, store):
    make_entry(store)
    store.set_status("D-0001", "unknown_current", reason="file_drift")
    assert store.get_entry("D-0001").status == "active"


def test_direct_sqlite_write_still_needs_the_guard(cfg, store):
    """The replay rule cannot see a direct index write; the guard is what blocks it (documented limit)."""
    make_entry(store)
    store.close()
    c = sqlite3.connect(cfg.index_path)
    c.execute("update entries set status='retired' where id='D-0001'")
    c.commit()
    c.close()
    assert _check(cfg, f"sqlite3 {cfg.index_path} \"update entries set status='retired' where id='D-0001'\"")


# ------------------------------------------------------------------ second round: review of the finished diff

def test_forged_supersede_of_a_user_entry_is_refused_on_replay(cfg, store):
    make_entry(store)
    store.close()
    e = _forge_and_rebuild(cfg, {"event": "supersede", "old": "D-0001", "new": None, "by": "agent"})
    assert e.status == "active"


def test_a_user_review_item_cannot_be_retired_without_the_user(cfg, store):
    e = make_entry(store)
    store._upsert(type(e).from_dict(e.to_dict() | {"status": "unknown_current"}))
    store._apply_event({"event": "status", "id": e.id, "status": "retired"})
    store._apply_event({"event": "entry_update", "id": e.id, "fields": {"status": "superseded"}})
    assert store.get_entry(e.id).status == "unknown_current"
    assert store.resolve(e.id, keep=False, by="user")
    assert store.get_entry(e.id).status == "retired"


def test_a_refused_add_does_not_overwrite_the_index(cfg, store):
    e = make_entry(store)
    forged = type(e).from_dict(e.to_dict() | {"statement": "Use MongoDB.", "provenance": {"source": "agent"}})
    try:
        store.add_entry(forged, embedding=[0.1, 0.2])
    except ValueError:
        pass
    assert store.get_entry(e.id).statement == "Use PostgreSQL as the primary database."


def test_guard_false_positives_on_project_files(cfg):
    for cmd in ["git rm --cached build/index.db", "cp fixtures/index.db /tmp/x.db", "touch config/calibration.json",
                'git commit -m "guard: deny rm of index.db"', "sed -i 's/ledger.jsonl/events.jsonl/' README.md",
                'sqlite3 app/index.db "create table t(x)"', f"cp {cfg.data_dir}/ledger.jsonl /tmp/backup.jsonl",
                f"python3 -c \"print(open('{cfg.data_dir}/ledger.jsonl').read()[:200])\""]:
        assert _check(cfg, cmd) is None, cmd


def test_guard_catches_stdin_sql_heredocs_inplace_and_cd(cfg):
    d = cfg.data_dir
    for cmd in [f"python3 - <<'PY'\nimport sqlite3\nsqlite3.connect('{d}/index.db').execute('delete from entries')\nPY",
                f"echo 'delete from entries;' | sqlite3 {d}/index.db", f"sqlite3 {d}/index.db < /tmp/x.sql",
                f"gawk -i inplace '!/D-0001/' {d}/ledger.jsonl", f"find {d} -name '*.db' -delete",
                f"cd {d} && sqlite3 index.db 'update entries set status=1'", f"cd {d.parent} && rm -rf epistemic",
                f"rm -rf {d.parent}", f"cp /tmp/forged.jsonl {d}/ledger.jsonl"]:
        assert _check(cfg, cmd), cmd


def test_guard_fails_closed_on_an_unresolvable_path(cfg, store, monkeypatch):
    _offline(monkeypatch, dm=FakeDM(default=0.05))
    store.close()
    out = _run("pre_tool_use", {"tool_name": "Write", "tool_use_id": "w", "tool_input": {
        "file_path": str(cfg.data_dir / "ledger.jsonl") + "\x00", "content": ""}}, cfg)
    assert _decision(out) == "deny"


def test_guard_runs_even_when_gate_tools_are_narrowed(cfg, store, monkeypatch):
    _offline(monkeypatch)
    store.close()
    monkeypatch.setenv("EPISTEMIC_GATE_TOOLS", '["Edit"]')
    assert _decision(_bash(cfg, f"rm {cfg.index_path}")) == "deny"
