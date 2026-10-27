from helpers import make_entry

from epistemic_monitor.gate import format_citation, run_tier0


def _pg_entry(store):
    return make_entry(store, scope={"paths": ["src/db/**", "src/db/*"], "tools": ["Edit", "Write", "Bash"]},
                      falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"\bimport\s+pymongo\b|\bMongoClient\b"},
                                  {"kind": "path_glob_forbidden", "glob": "src/db/mongo/*"},
                                  {"kind": "command_regex_forbidden", "pattern": r"pip install (pymongo|motor)"}],
                      anchors=["src/db/client.py", "PostgreSQL"])


def test_edit_trips_diff_regex(store, tmp_path):
    e = _pg_entry(store)
    r = run_tier0(store.active_entries(), "Edit",
                  {"file_path": str(tmp_path / "src/db/client.py"), "old_string": "x", "new_string": "from pymongo import MongoClient"},
                  str(tmp_path))
    assert r.scoped_entries == [e.id]
    assert [t.kind for t in r.trips] == ["diff_regex_forbidden"]
    text = format_citation({e.id: e}, r.trips)
    assert e.id in text and "SUPERSEDE" in text


def test_consistent_edit_passes(store, tmp_path):
    _pg_entry(store)
    r = run_tier0(store.active_entries(), "Edit",
                  {"file_path": "src/db/client.py", "old_string": "x", "new_string": "engine = create_engine(URL, pool_size=10)"},
                  str(tmp_path))
    assert r.trips == [] and r.decision == "allow"


def test_out_of_scope_path_is_ignored(store, tmp_path):
    _pg_entry(store)
    r = run_tier0(store.active_entries(), "Edit",
                  {"file_path": "src/api/routes.py", "old_string": "x", "new_string": "from pymongo import MongoClient"},
                  str(tmp_path))
    assert r.scoped_entries == [] and r.trips == []


def test_bash_command_regex_and_classifier(store, tmp_path):
    e = _pg_entry(store)
    # Global-scope constraint so the Bash command is in scope regardless of paths.
    make_entry(store, type="constraint", statement="Never install Mongo drivers.",
               falsifiers=[{"kind": "command_regex_forbidden", "pattern": r"pip install (pymongo|motor)"}])
    r = run_tier0(store.active_entries(), "Bash", {"command": "pip install pymongo"}, str(tmp_path))
    assert r.classification is not None and r.classification.verdict == "no_source_write"
    assert any(t.kind == "command_regex_forbidden" and t.entry_id == "C-0001" for t in r.trips)
    assert e.id not in r.scoped_entries  # path-scoped entry, command touches no path


def test_new_file_requires_companion(store, tmp_path):
    make_entry(store, type="process", statement="Every new API module gets a test file.",
               falsifiers=[{"kind": "path_glob_required_with", "glob": "src/api/*.py", "requires": "tests/api/test_{stem}.py"}])
    r = run_tier0(store.active_entries(), "Write", {"file_path": "src/api/users.py", "content": "x = 1"}, str(tmp_path))
    assert r.change.is_new_file and [t.kind for t in r.trips] == ["path_glob_required_with"]
    (tmp_path / "tests/api").mkdir(parents=True)
    (tmp_path / "tests/api/test_users.py").write_text("")
    r = run_tier0(store.active_entries(), "Write", {"file_path": "src/api/users.py", "content": "x = 1"}, str(tmp_path))
    assert r.trips == []
