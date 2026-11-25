"""diff_regex_forbidden counts only what a change adds (arXiv 2611.29916: rules are often broken by older code on
the day they are written; 2611.11455: agents copy existing violations, so copies still count)."""
from pathlib import Path

from fakes import FakeDM
from helpers import make_entry

from epistemic_monitor.gate import GatePipeline, change_from_tool_input, run_tier0

LEGACY = "import os\nimport pymongo\n\nclient = pymongo.MongoClient(URL)\n"
PAT = r"\bimport\s+pymongo\b|\bMongoClient\b"


def _rule(store):
    return make_entry(store, type="constraint", statement="Use PostgreSQL; never add MongoDB code.",
                      scope={"paths": ["src/db/*"], "tools": ["Edit", "Write", "MultiEdit", "Bash"]},
                      falsifiers=[{"kind": "diff_regex_forbidden", "pattern": PAT}])


def _legacy(cfg) -> Path:
    p = Path(cfg.cwd) / "src/db/client.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(LEGACY)
    return p


def _t0(store, cfg, tool, ti):
    return run_tier0(store.active_entries(), tool, ti, str(cfg.cwd))


def test_write_rewriting_legacy_file_without_adding_is_allowed(cfg, store):
    _rule(store); p = _legacy(cfg)
    r = _t0(store, cfg, "Write", {"file_path": str(p), "content": '"""Database client."""\n' + LEGACY})
    assert r.trips == []


def test_write_adding_another_copy_trips(cfg, store):
    e = _rule(store); p = _legacy(cfg)
    r = _t0(store, cfg, "Write", {"file_path": str(p), "content": LEGACY + "backup = pymongo.MongoClient(URL2)\n"})
    assert [t.entry_id for t in r.trips] == [e.id] and "+1 'MongoClient'" in r.trips[0].detail


def test_edit_with_legacy_line_as_context_is_allowed(cfg, store):
    _rule(store); _legacy(cfg)
    r = _t0(store, cfg, "Edit", {"file_path": "src/db/client.py", "old_string": "import os\nimport pymongo\n",
                                 "new_string": "import os\nimport sys\nimport pymongo\n"})
    assert r.trips == []


def test_edit_duplicating_match_trips(cfg, store):
    _rule(store); _legacy(cfg)
    r = _t0(store, cfg, "Edit", {"file_path": "src/db/client.py", "old_string": "client = pymongo.MongoClient(URL)\n",
                                 "new_string": "client = pymongo.MongoClient(URL)\nother = pymongo.MongoClient(URL)\n"})
    assert len(r.trips) == 1


def test_edit_introducing_match_trips(cfg, store):
    _rule(store)
    r = _t0(store, cfg, "Edit", {"file_path": "src/db/x.py", "old_string": "x", "new_string": "import pymongo"})
    assert len(r.trips) == 1


def test_replace_all_scales_both_sides(cfg, store):
    _rule(store)
    r = _t0(store, cfg, "Edit", {"file_path": "src/db/x.py", "old_string": "MongoClient(a)", "new_string": "MongoClient(b)",
                                 "replace_all": True})
    assert r.trips == []


def test_multiedit_is_checked_per_edit(cfg, store):
    _rule(store)
    r = _t0(store, cfg, "MultiEdit", {"file_path": "src/db/x.py", "edits": [
        {"old_string": "pass", "new_string": "import pymongo"},
        {"old_string": "import pymongo", "new_string": "import pymongo  # db"}]})
    assert len(r.trips) == 1


def test_emptied_file_then_write_back_trips(cfg, store):
    _rule(store); p = _legacy(cfg)
    p.write_text("")
    r = _t0(store, cfg, "Write", {"file_path": str(p), "content": LEGACY})
    assert len(r.trips) == 1


def test_unreadable_or_huge_pre_image_checks_whole_text(cfg, store, monkeypatch):
    import epistemic_monitor.gate as g
    _rule(store); p = _legacy(cfg)
    monkeypatch.setattr(g, "PRE_IMAGE_MAX_BYTES", 5)
    r = _t0(store, cfg, "Write", {"file_path": str(p), "content": LEGACY})
    assert len(r.trips) == 1
    monkeypatch.setattr(g, "PRE_IMAGE_MAX_BYTES", 2_000_000)
    p.write_bytes(b"\xff\xfe import pymongo")
    r = _t0(store, cfg, "Write", {"file_path": str(p), "content": LEGACY})
    assert len(r.trips) == 1


def test_same_file_swap_is_allowed_documented_limit(cfg, store):
    _rule(store); p = _legacy(cfg)
    r = _t0(store, cfg, "Write", {"file_path": str(p), "content": LEGACY.replace("MongoClient(URL)", "MongoClient(URL2)")})
    assert r.trips == []


def test_new_file_unchanged(cfg, store):
    make_entry(store, type="constraint", statement="New API modules declare __all__.",
               scope={"paths": ["src/api/*"]}, falsifiers=[{"kind": "diff_regex_required", "pattern": r"__all__"}])
    _rule(store)
    r = _t0(store, cfg, "Write", {"file_path": "src/db/new.py", "content": LEGACY})
    assert len(r.trips) == 1
    r = _t0(store, cfg, "Write", {"file_path": "src/api/new.py", "content": "x = 1\n"})
    assert [t.kind for t in r.trips] == ["diff_regex_required"]


def test_bash_heredoc_unchanged(cfg, store):
    _rule(store); _legacy(cfg)
    r = _t0(store, cfg, "Bash", {"command": "cat > src/db/client.py <<'EOF'\nimport pymongo\nEOF"})
    assert len(r.trips) == 1


def test_legacy_rewrites_do_not_spend_deny_budget(cfg, store):
    """Before: five rewrites of a legacy file were denied and exhausted the session budget, so a real violation of
    another user rule came back advisory."""
    _rule(store); p = _legacy(cfg)
    other = make_entry(store, type="constraint", statement="Never call eval.",
                       falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"\beval\("}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.0), llm=None)
    for i in range(6):
        res = pipe.evaluate("Write", {"file_path": str(p), "content": f"# rev {i}\n" + LEGACY}, "s1")
        assert res.decision == "allow", res.reason
    res = pipe.evaluate("Edit", {"file_path": "src/app.py", "old_string": "x", "new_string": "eval(data)"}, "s1")
    assert res.decision == "deny" and other.id in res.conflicts


def test_tier2_sees_a_diff_not_the_whole_file(cfg, store):
    _rule(store); p = _legacy(cfg)
    dm = FakeDM(default=0.0)
    pipe = GatePipeline(store, cfg, dm=dm, llm=None)
    pipe.evaluate("Write", {"file_path": str(p), "content": '"""Database client."""\n' + LEGACY}, "s1")
    diffs = [c["state"]["planned_change"]["diff"] for c in dm.calls if isinstance(c["state"], dict)]
    assert diffs and all(d.startswith("--- a/") and '+"""Database client."""' in d for d in diffs)
    assert all("+import pymongo" not in d for d in diffs)


def test_change_diff_text_for_edit():
    ch = change_from_tool_input("Edit", {"file_path": "a.py", "old_string": "x = 1\n", "new_string": "x = 2\n"}, "/tmp")
    assert "-x = 1" in ch.diff_text() and "+x = 2" in ch.diff_text()
