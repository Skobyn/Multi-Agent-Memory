from epistemic_monitor.pinning import render_pinned, rank_entries

from .helpers import make_entry


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
    assert old.id in out.split("more not shown")[1]
