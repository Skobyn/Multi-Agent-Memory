"""A supersession keeps the words that authorized it, and a model-named turn uuid must exist
(docs/RESEARCH-2026-11-21.md, idea D shrunk)."""
import json

from fakes import FakeLLM
from helpers import make_entry
from test_extractor import _turns

from epistemic_monitor import hooks
from epistemic_monitor.extractor import Extractor
from epistemic_monitor.store import LedgerStore


def _events(cfg):
    return [json.loads(x) for x in cfg.ledger_path.read_text().splitlines() if x.strip()]


def test_user_supersede_keeps_its_evidence_on_the_event_and_the_entry(cfg, store):
    make_entry(store, type="constraint", statement="Never send email on the user's behalf; draft only.")
    quote = "never send email on my behalf"   # the user's own words in _turns()
    llm = FakeLLM([{"entries": [], "supersedes": [{"old": "C-0001", "reason": "user relaxed it", "by": "user",
                                                    "evidence": quote, "turn_uuid": "u-1"}]}])
    res = Extractor(store, llm, None).run(_turns(), "s1")
    assert res.superseded[0]["ok"] is True
    old = store.get_entry("C-0001")
    assert old.status == "superseded" and old.revoked_by == "user" and old.revoked_evidence == quote
    ev = [e for e in _events(cfg) if e.get("event") == "supersede"][-1]
    assert ev["evidence"] == quote and ev["turn_uuid"] == "u-1"
    store.close()
    replay = LedgerStore(cfg)          # survives a rebuild of the projection from the event log
    try:
        replay._conn.execute("DELETE FROM entries")
        replay._conn.commit()
        replay.close()
        replay = LedgerStore(cfg)
        assert replay.get_entry("C-0001").revoked_evidence == quote
    finally:
        replay.close()


def test_invented_turn_uuids_are_not_stored(store):
    entry = {"type": "decision", "statement": "Use PostgreSQL as the primary database.", "source": "user",
             "evidence": "Let's use PostgreSQL", "turn_uuid": "u-999", "confidence": 0.9}
    make_entry(store, type="constraint", statement="Never send email on the user's behalf; draft only.", source="agent")
    sup = {"old": "C-0001", "reason": "r", "by": "agent", "evidence": "x", "turn_uuid": "made-up"}
    Extractor(store, FakeLLM([{"entries": [entry], "supersedes": [sup]}]), None).run(_turns(), "s1")
    added = [e for e in store.entries() if e.type == "decision"][0]
    assert added.provenance.turn_uuid == ""
    ok = {"type": "decision", "statement": "Never send email on my behalf, draft only please.", "source": "user",
          "evidence": "never send email on my behalf", "turn_uuid": "u-1", "confidence": 0.9}
    res = Extractor(store, FakeLLM([{"entries": [ok], "supersedes": []}]), None).run(_turns(), "s1")
    assert res.added and all(e.provenance.turn_uuid == "u-1" for e in res.added)


def test_supersede_line_keeps_the_line(cfg, store):
    make_entry(store)
    hooks._apply_supersede_lines("ok then.\nSUPERSEDE D-0001: we moved to MySQL", "user", store, "s1")
    e = store.get_entry("D-0001")
    assert e.status == "superseded" and e.revoked_evidence == "SUPERSEDE D-0001: we moved to MySQL"


def test_entailment_counts_a_rule_reported_as_discharged_as_dropped():
    # CompAct-Bench (2611.25673): "ran pytest, all green" is not "run pytest before every commit" (idea B, shrunk)
    from epistemic_monitor.audit import ENTAILMENT_CRITERIA
    assert "already discharged or no longer applying" in ENTAILMENT_CRITERIA["false"]
