from epistemic_monitor.pinning import render_pinned
from epistemic_monitor.schema import Entry, Provenance


def _e(i, statement="Never do X."):
    return Entry(id=f"C-{i:04d}", type="constraint", statement=statement, provenance=Provenance(source="user"))


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
    e = _e(1, "Never touch billing.")
    e.evidence_unverified = True
    text = render_pinned([e])
    assert "**C-0001**" in text and "[unverified" in text
