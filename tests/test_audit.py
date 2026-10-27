from fakes import FakeDM
from helpers import make_entry

from epistemic_monitor.audit import behavior_watch, dropped_by_entailment, entailment_audit


def test_entailment_audit_scores_and_outages(store):
    a = make_entry(store)
    b = make_entry(store, statement="Use Redis for caching.")
    c = make_entry(store, statement="Run pytest before every commit.", type="process")
    dm = FakeDM(by_id={a.id: 0.95, b.id: 0.2}, fail_ids={c.id})
    scores = entailment_audit(store.active_entries(), "summary mentions postgres", dm)
    assert scores[a.id] == 0.95 and scores[b.id] == 0.2 and scores[c.id] is None
    assert dropped_by_entailment(scores, 0.5) == {b.id}
    assert all("decision" in call["state"] and "summary" in call["state"] for call in dm.calls)


def test_entailment_audit_empty_inputs(store):
    assert entailment_audit([], "x", FakeDM()) == {}
    e = make_entry(store)
    assert entailment_audit([e], "   ", FakeDM()) == {e.id: None}


def test_behavior_watch_state_shape(store):
    e = make_entry(store)
    dm = FakeDM(default=0.8)
    p, ms = behavior_watch([e], [{"tool_name": "Bash", "tool_input": {"command": "pip install pymongo", "description": "x"}},
                                 {"tool_name": "Edit", "tool_input": {"file_path": "src/db/client.py", "new_string": "y" * 1000}}], dm)
    assert p == 0.8
    state = dm.calls[0]["state"]
    assert state["entries"][0]["id"] == e.id
    assert state["tool_calls"][0] == {"tool": "Bash", "input": {"command": "pip install pymongo"}}
    assert len(state["tool_calls"][1]["input"]["new_string"]) == 400
