from helpers import make_entry

from epistemic_monitor.drift import drift_check, fingerprint, rebaseline
from epistemic_monitor.feedback import feedback_report


def _premise(store, root, source="agent", **kw):
    (root / "a.py").write_text("x = 1\n")
    e = make_entry(store, type="premise", statement="a.py defines x", source=source, confirmed=False,
                   scope={"paths": ["a.py"]}, **kw)
    store._apply_event({"event": "entry_update", "id": e.id, "fields": {"scope_fingerprint": fingerprint(root, ["a.py"])}})
    return e


def test_fingerprint_skips_globs_and_escapes(tmp_path):
    (tmp_path / "f.txt").write_text("hi")
    assert set(fingerprint(tmp_path, ["f.txt", "src/**", "../x"])) == {"f.txt"}


def test_drift_moves_agent_premise_and_rebaseline_restores(store, cfg):
    e = _premise(store, cfg.cwd)
    assert drift_check(store, cfg.cwd) == []
    (cfg.cwd / "a.py").write_text("x = 2\n")
    assert drift_check(store, cfg.cwd) == [e.id]
    assert store.get_entry(e.id).status == "unknown_current"
    assert store.resolve(e.id, keep=True)
    rebaseline(store, cfg.cwd, e.id)
    assert drift_check(store, cfg.cwd) == []


def test_drift_never_touches_user_entries(store, cfg):
    e = _premise(store, cfg.cwd, source="user")
    (cfg.cwd / "a.py").write_text("changed")
    assert drift_check(store, cfg.cwd) == []
    assert store.get_entry(e.id).status == "active"


def test_feedback_report_is_read_only_signal():
    gates = [{"kind": "gate", "flagged": ["D-0001"], "tool_use_id": f"t{i}"} for i in range(3)]
    ivs = gates + [{"kind": "supersede", "entry": "D-0001", "by": "user", "ok": True}]
    rep = feedback_report(ivs, [{"tool_use_id": "t0", "label": "ok"}, {"tool_use_id": "t1", "label": "ok"}])
    assert rep["candidates_for_review"] == [{"entry": "D-0001", "flags": 3}]
    assert rep["tighten"][0]["entry"] == "D-0001"
    assert feedback_report(gates[:2])["candidates_for_review"] == []
