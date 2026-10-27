from pathlib import Path

from epistemic_monitor.schema import Entry, Provenance, Scope

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def make_entry(store, type="decision", statement="Use PostgreSQL as the primary database.",
               source="user", confirmed=True, **kw) -> Entry:
    e = Entry(id=store.next_id(type), type=type, statement=statement,
              provenance=Provenance(source=source, confirmed_by_user=confirmed, session_id="s1"),
              scope=Scope(**kw.pop("scope", {})), **kw)
    return store.add_entry(e)
