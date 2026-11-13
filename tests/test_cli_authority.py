"""Ledger CLI paths that could hand an agent (which has Bash) user authority over the ledger."""
from helpers import make_entry

from epistemic_monitor import cli
from epistemic_monitor.store import LedgerStore


def _cli(cfg, monkeypatch, *argv):
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    return cli.main(["ledger", *argv])


def test_resolve_retire_of_user_entry_needs_by_user(cfg, store, monkeypatch):
    old = make_entry(store, statement="Use PostgreSQL.", scope={"paths": ["src/db/*"]})
    same = make_entry(store, type="constraint", statement="Queries go through the ORM.", scope={"paths": ["src/db/*"]})
    assert store.supersede(old.id, None, "moving", by="user")
    store.close()
    _cli(cfg, monkeypatch, "resolve", same.id, "--retire")
    s = LedgerStore(cfg)
    try:
        assert s.get_entry(same.id).status == "unknown_current"
    finally:
        s.close()
    _cli(cfg, monkeypatch, "resolve", same.id, "--retire", "--by", "user")
    s = LedgerStore(cfg)
    try:
        assert s.get_entry(same.id).status == "retired"
    finally:
        s.close()


def test_bulk_retire_of_user_entries_is_refused(cfg, store, monkeypatch, capsys):
    u = make_entry(store, type="constraint", statement="Never force-push to main.")
    a = make_entry(store, statement="Use ruff for linting.", source="agent", confirmed=False)
    store.close()
    _cli(cfg, monkeypatch, "retire", "--provenance", "user", "--reason", "policy update", "--by", "user")
    assert "refused" in capsys.readouterr().out
    _cli(cfg, monkeypatch, "retire", "--provenance", "agent", "--reason", "cleanup")
    s = LedgerStore(cfg)
    try:
        assert s.get_entry(u.id).status == "active" and s.get_entry(a.id).status == "retired"
    finally:
        s.close()


def test_dedupe_keeps_the_user_entry(cfg, store, monkeypatch):
    a = make_entry(store, type="constraint", statement="Never edit applied migrations.", source="agent", confirmed=False)
    u = make_entry(store, type="constraint", statement="Never edit applied migrations!")
    store.close()
    _cli(cfg, monkeypatch, "dedupe", "--apply")
    s = LedgerStore(cfg)
    try:
        assert s.get_entry(u.id).status == "active"
        assert s.get_entry(a.id).status == "superseded" and s.get_entry(a.id).superseded_by == u.id
    finally:
        s.close()
