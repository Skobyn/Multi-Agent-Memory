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


def test_replace_all_counts_every_occurrence(cfg, store):
    _rule(store); p = _legacy(cfg)
    p.write_text(LEGACY + "x = 1\ny = 1\n")
    r = _t0(store, cfg, "Edit", {"file_path": "src/db/client.py", "old_string": "x = 1", "new_string": "x = 2",
                                 "replace_all": True})
    assert r.trips == []
    r = _t0(store, cfg, "Edit", {"file_path": "src/db/client.py", "old_string": "= 1", "new_string": "= MongoClient()",
                                 "replace_all": True})
    assert len(r.trips) == 1 and "+2" in r.trips[0].detail


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


def test_same_file_swap_trips(cfg, store):
    """Matches are keyed by their line, so rewriting the line that holds a violation counts as adding one."""
    _rule(store); p = _legacy(cfg)
    r = _t0(store, cfg, "Write", {"file_path": str(p), "content": LEGACY.replace("MongoClient(URL)", "MongoClient(URL2)")})
    assert len(r.trips) == 1


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


def test_change_diff_text_for_edit(tmp_path):
    (tmp_path / "a.py").write_text("import psycopg2\nconn = psycopg2.connect(DSN)")   # no final newline
    ch = change_from_tool_input("Edit", {"file_path": "a.py", "old_string": "import psycopg2\nconn = psycopg2.connect(DSN)",
                                         "new_string": "import pymongo\nconn = pymongo.MongoClient(URL)"}, str(tmp_path))
    d = ch.diff_text()
    lines = d.splitlines()
    assert "+import pymongo" in lines and "-conn = psycopg2.connect(DSN)" in lines   # not glued onto one line
    assert ch.diff_text() is ch._diff or ch.diff_text() == d   # cached


def test_diff_falls_back_when_empty_or_huge(tmp_path, monkeypatch):
    import epistemic_monitor.gate as g
    (tmp_path / "a.py").write_text("x = 1\n")
    same = change_from_tool_input("Write", {"file_path": "a.py", "content": "x = 1\n"}, str(tmp_path))
    assert same.diff_text() == "x = 1\n"
    monkeypatch.setattr(g, "DIFF_MAX_CHARS", 3)
    big = change_from_tool_input("Write", {"file_path": "a.py", "content": "x = 2\n"}, str(tmp_path))
    assert big.diff_text() == "x = 2\n"


def test_zero_width_pattern_still_trips(cfg, store):
    make_entry(store, type="constraint", statement="Never call eval.",
               falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"(?=eval\()"}])
    r = _t0(store, cfg, "Write", {"file_path": "src/new.py", "content": "x = eval(y)\n"})
    assert len(r.trips) == 1
    p = Path(cfg.cwd) / "src/old.py"; p.parent.mkdir(parents=True, exist_ok=True); p.write_text("a = 1\n")
    r = _t0(store, cfg, "Edit", {"file_path": "src/old.py", "old_string": "a = 1", "new_string": "a = eval(y)"})
    assert len(r.trips) == 1


def test_comment_cannot_cancel_a_new_violation(cfg, store):
    make_entry(store, type="constraint", statement="Never call eval.",
               falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"eval\("}])
    p = Path(cfg.cwd) / "src/m.py"; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# do not call eval( here\nx = 1\n")
    r = _t0(store, cfg, "Edit", {"file_path": "src/m.py", "old_string": "# do not call eval( here\nx = 1",
                                 "new_string": "# safe\nx = eval(user_input)"})
    assert len(r.trips) == 1
    r = _t0(store, cfg, "Write", {"file_path": str(p), "content": "x = eval(user_input)\n"})
    assert len(r.trips) == 1


def test_anchor_sees_real_line_starts(cfg, store):
    make_entry(store, type="constraint", statement="No top-level eval.",
               falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"^eval\("}])
    p = Path(cfg.cwd) / "src/m.py"; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("y = eval(z)\n")
    r = _t0(store, cfg, "Edit", {"file_path": "src/m.py", "old_string": "eval(z)", "new_string": "safe(z)\neval(w)"})
    assert len(r.trips) == 1


def test_edit_that_does_not_apply_checks_new_text(cfg, store):
    _rule(store); _legacy(cfg)
    r = _t0(store, cfg, "Edit", {"file_path": "src/db/client.py", "old_string": "not in file",
                                 "new_string": "import pymongo"})
    assert len(r.trips) == 1
