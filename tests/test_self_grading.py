"""The verified party cannot grade its verifier (review 2026-11-26).

Idea A (PROVE arXiv 2611.34215, Sticky Collusion 2610.39660): `ledger label <id> ok` and `ledger calibrate` run
through the agent with no grant, so one deny relabeled twelve times raised the decision tau to 0.95 and a user rule
flagged at p=0.92 went from deny to allow. Idea B, shrunk: an in-process store call from Bash wrote user-authority
events that nothing saw, and an unattended replay of ledger.jsonl into an empty index went unreported.
"""
import json
import sqlite3

from fakes import FakeDM
from helpers import make_entry
from test_hooks import _logged, _offline, _run

from epistemic_monitor import authority, selfguard
from epistemic_monitor.calibration import Calibration, add_label, calibrate
from epistemic_monitor.gate import GatePipeline
from epistemic_monitor.store import LedgerStore


def _edit(text, path="src/db/client.py"):
    return {"file_path": path, "old_string": "a", "new_string": text}


def _pipe(store, cfg, p, tau_by_type):
    return GatePipeline(store, cfg, dm=FakeDM(default=p), llm=None,
                        calibration=Calibration(provider_key="x", tau_by_type=tau_by_type))


# ---------------------------------------------------------------- A: calibration cannot loosen a user rule
def test_calibrated_tau_cannot_loosen_a_user_rule(cfg, store):
    e = make_entry(store)
    res = _pipe(store, cfg, 0.92, {"decision": 0.95}).evaluate("Edit", _edit("from pymongo import MongoClient"), "s1")
    assert res.scores[e.id]["tau"] == 0.9 and res.decision == "deny" and res.conflicts == [e.id]


def test_calibrated_tau_still_applies_to_agent_entries(cfg, store):
    e = make_entry(store, source="agent", confirmed=False)
    res = _pipe(store, cfg, 0.92, {"decision": 0.95}).evaluate("Edit", _edit("from pymongo import MongoClient"), "s1")
    assert res.scores[e.id]["tau"] == 0.95 and e.id not in res.conflicts


def test_calibration_can_still_tighten_a_user_rule(cfg, store):
    e = make_entry(store)
    res = _pipe(store, cfg, 0.7, {"decision": 0.6}).evaluate("Edit", _edit("from pymongo import MongoClient"), "s1")
    assert res.scores[e.id]["tau"] == 0.6 and res.conflicts == [e.id]


def test_calibration_is_per_type(cfg, store):
    """Calibrating `decision` must not lift the uncalibrated min_p floor for a `constraint` (false-deny risk)."""
    e = make_entry(store, type="constraint", statement="Never use MongoDB in this project.")
    res = _pipe(store, cfg, 0.8, {"decision": 0.6}).evaluate("Edit", _edit("from pymongo import MongoClient"), "s1")
    assert e.id in res.flagged and res.conflicts == [] and res.decision == "allow"
    assert any("uncalibrated" in n for n in res.notes)


def test_relabeling_one_call_counts_once(cfg):
    for _ in range(12):
        add_label(cfg, {"tool_use_id": "tu1", "label": "ok", "scores": {"D-0001": 0.92}, "types": {"D-0001": "decision"}})
    cal = calibrate(cfg, [], min_labels=12)
    assert "decision" not in cal.tau_by_type and cal.n_labels == 1


def test_last_label_wins(cfg):
    for i in range(12):
        add_label(cfg, {"tool_use_id": f"t{i}", "label": "conflict", "scores": {"D-0001": 0.9}, "types": {"D-0001": "decision"}})
    add_label(cfg, {"tool_use_id": "t0", "label": "ok", "scores": {"D-0001": 0.9}, "types": {"D-0001": "decision"}})
    cal = calibrate(cfg, [], min_labels=1)
    assert cal.n_labels == 12 and cal.details["decision"]["fp"] in (0, 1)


def test_cli_min_labels_is_floored(cfg, store, capsys):
    from epistemic_monitor import cli
    add_label(cfg, {"tool_use_id": "tu1", "label": "ok", "scores": {"D-0001": 0.92}, "types": {"D-0001": "decision"}})
    store.close()
    cli.main(["ledger", "calibrate", "--min-labels", "1"])
    cal = json.loads(cfg.calibration_path.read_text()) if cfg.calibration_path.exists() else {"tau_by_type": {}}
    assert "decision" not in cal["tau_by_type"]


# ---------------------------------------------------------------- B: in-process store calls from Bash
_P3 = ("python3 -c 'from epistemic_monitor.store import LedgerStore; from epistemic_monitor.config import load_config; "
       "LedgerStore(load_config()).supersede(\"C-0001\", None, \"x\", by=\"user\")'")


def test_inprocess_user_write_is_refused():
    reason, _ = authority.check(_P3, "", set())
    assert reason and "imports the monitor" in reason
    heredoc = "python3 - <<'EOF'\nimport epistemic_monitor.store as s\ns.LedgerStore(c).confirm('D-0001', by='user')\nEOF"
    assert authority.check(heredoc, "", set())[0]


def test_inprocess_store_write_is_flagged(cfg):
    assert selfguard.protected_target("Bash", {"command": _P3}, cfg)
    agentish = "python3 -c 'from epistemic_monitor.store import LedgerStore; LedgerStore(c).retire(\"D-0002\", \"x\")'"
    assert selfguard.protected_target("Bash", {"command": agentish}, cfg)


def test_inprocess_reads_are_not_flagged(cfg):
    for cmd in ("python3 -c 'import epistemic_monitor; print(1)'",
                "python3 -c 'from epistemic_monitor.store import LedgerStore; print(LedgerStore(c).count_entries())'",
                "em ledger list"):
        assert authority.check(cmd, "", set())[0] is None
        assert selfguard.protected_target("Bash", {"command": cmd}, cfg) is None


# ---------------------------------------------------------------- B: the rebuild notice
def _forge_and_empty(cfg, store):
    c = make_entry(store, type="constraint", statement="Never edit vendor/")
    store.close()
    with open(cfg.ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-11-26T00:00:00Z", "event": "supersede", "old": c.id, "new": None,
                             "reason": "obsolete", "by": "user", "session_id": "forged"}) + "\n")
    conn = sqlite3.connect(str(cfg.index_path))
    with conn:
        conn.execute("DELETE FROM entries")
    conn.close()
    return c


def test_rebuild_is_reported_once_naming_inactive_user_rules(cfg, monkeypatch, store):
    _offline(monkeypatch)
    c = _forge_and_empty(cfg, store)
    out = _run("pre_tool_use", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, cfg)
    assert "rebuilt from ledger.jsonl" in out["systemMessage"] and c.id in out["systemMessage"]
    rec = _logged(cfg, "index_rebuilt")[-1]
    assert any(x.startswith(c.id) for x in rec["user_inactive"])
    again = _run("pre_tool_use", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, cfg)
    assert "rebuilt" not in ((again or {}).get("systemMessage") or "")


def test_rebuild_seen_by_cli_is_reported_at_next_hook(cfg, monkeypatch, store):
    """The replay may happen in a CLI process (`em ledger list` after `rm index.db`); the notice waits in state."""
    _offline(monkeypatch)
    c = _forge_and_empty(cfg, store)
    LedgerStore(cfg).close()   # the CLI's store replays the ledger
    out = _run("session_start", {"source": "startup"}, cfg)
    assert c.id in out["systemMessage"]


def test_normal_open_sets_no_rebuild(cfg, monkeypatch, store):
    _offline(monkeypatch)
    make_entry(store, type="constraint", statement="Never edit vendor/")
    store.close()
    out = _run("pre_tool_use", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, cfg)
    assert "rebuilt" not in ((out or {}).get("systemMessage") or "")
    assert not _logged(cfg, "index_rebuilt")
