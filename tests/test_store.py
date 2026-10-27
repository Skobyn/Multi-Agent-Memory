import pytest
from helpers import make_entry

from epistemic_monitor.schema import Entry, Provenance
from epistemic_monitor.store import LedgerStore, cosine


def test_ids_and_roundtrip(store):
    e = make_entry(store)
    assert e.id == "D-0001"
    assert make_entry(store, type="constraint", statement="Never commit secrets.").id == "C-0001"
    got = store.get_entry("D-0001")
    assert got is not None and got.statement == e.statement and got.provenance.source == "user"
    assert store.count_entries() == 2 and len(store.active_entries()) == 2


def test_validation_rejects_bad_entries(store):
    with pytest.raises(ValueError):
        store.add_entry(Entry(id="X-1", type="decision", statement="bad id"))
    with pytest.raises(ValueError):
        store.add_entry(Entry(id="D-0009", type="decision", statement="bad falsifier", falsifiers=[{"kind": "nope"}]))


def test_keyword_search(store):
    make_entry(store)
    make_entry(store, statement="Use Redis for caching.")
    hits = store.search_keyword("which database should we use", top_k=5)
    assert hits and hits[0][0].id == "D-0001"


def test_vector_search(store):
    a = make_entry(store)
    b = make_entry(store, statement="Use Redis for caching.")
    store.set_embedding(a.id, [1.0, 0.0, 0.0])
    store.set_embedding(b.id, [0.0, 1.0, 0.0])
    hits = store.search_vector([0.9, 0.1, 0.0], top_k=2)
    assert [h[0].id for h in hits] == [a.id, b.id]
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0) and cosine([], [1]) == 0.0


def test_supersede_authority_rule_and_propagation(store):
    user_decision = make_entry(store)  # user provenance
    dependent = make_entry(store, statement="Use Alembic for migrations.", source="agent", confirmed=False,
                           depends_on=[user_decision.id])
    # An agent may not supersede a user decision.
    assert store.supersede(user_decision.id, None, "agent changed its mind", by="agent") is False
    assert store.get_entry(user_decision.id).status == "active"
    assert any(r["kind"] == "supersede_refused" for r in store.recent_interventions())
    # The user may; dependents become orphaned.
    assert store.supersede(user_decision.id, None, "moving to Mongo", by="user") is True
    assert store.get_entry(user_decision.id).status == "superseded"
    assert store.get_entry(dependent.id).status == "orphaned"


def test_rebuild_from_events(cfg, store):
    make_entry(store)
    make_entry(store, statement="Run the tests before every commit.", type="process")
    store.supersede("D-0001", None, "changed", by="user")
    store.close()
    cfg.index_path.unlink()
    fresh = LedgerStore(cfg)
    try:
        assert fresh.count_entries() == 2
        assert fresh.get_entry("D-0001").status == "superseded"
        assert fresh.get_entry("R-0001").status == "active"
        assert fresh.next_id("decision") == "D-0002"  # counters recovered after rebuild
    finally:
        fresh.close()


def test_watermark_and_state(store):
    assert store.get_watermark("s1") is None
    store.set_watermark("s1", "u-123")
    assert store.get_watermark("s1") == "u-123"
    store.set_state("s1", "denies", 3)
    assert store.get_state("s1", "denies", 0) == 3
