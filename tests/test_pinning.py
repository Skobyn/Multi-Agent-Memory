from epistemic_monitor.pinning import render_pinned, rank_entries
from epistemic_monitor.schema import Entry, Provenance
from helpers import make_entry


def test_hard_tier_is_never_truncated(store):
    for i in range(6):
        make_entry(store, type="constraint", statement=f"Never touch module {i}. " + "x" * 200)
    out = render_pinned(store.active_entries(), None, max_entries=2, max_chars=600)
    for i in range(6):
        assert f"Never touch module {i}." in out


def test_long_entry_does_not_hide_shorter_ones(store):
    make_entry(store, type="premise", statement="long " * 400, source="agent", confirmed=False)
    short = make_entry(store, type="premise", statement="Short premise.", source="agent", confirmed=False)
    out = render_pinned(store.active_entries(), None, max_entries=25, max_chars=1500)
    assert short.id in out and "run `/ledger list`" in out


def test_omitted_ids_listed_and_newest_wins_ties(store):
    old = make_entry(store, type="premise", statement="Old premise.", source="agent", confirmed=False)
    new = make_entry(store, type="premise", statement="New premise.", source="agent", confirmed=False)
    new.valid_from = "2099-01-01T00:00:00Z"
    ranked = rank_entries([old, new])
    assert ranked[0].id == new.id
    out = render_pinned([old, new], None, max_entries=1)
    assert old.id in out.split("not shown (pin cap)")[1]


def _e(i, statement="Never do X.", source="agent"):
    return Entry(id=f"C-{i:04d}", type="constraint", statement=statement, provenance=Provenance(source=source))


def test_omitted_entries_are_named_not_counted():
    entries = [_e(i, f"Rule number {i} " + "x" * 300) for i in range(1, 9)]
    text = render_pinned(entries, max_entries=3)
    assert "5 not shown (pin cap)" in text
    tail = text.split("not shown")[1]
    omitted = [e.id for e in entries if f"**{e.id}**" not in text]
    assert len(omitted) == 5 and all(i in tail for i in omitted)


def test_nothing_omitted_no_tail():
    assert "not shown" not in render_pinned([_e(1)])


def test_unverified_user_entry_is_marked_but_still_pinned():
    e = _e(1, "Never touch billing.", source="user")
    e.evidence_unverified = True
    text = render_pinned([e])
    assert "**C-0001**" in text and "[unverified" in text
