"""2026-11-27 triage: Bash write targets reach path falsifiers; the agent cannot calibrate its own gate; a handoff
marks a rule superseded while the subagent ran (docs/RESEARCH-2026-11-27.md)."""
import json

import pytest
from fakes import FakeDM
from helpers import make_entry
from test_hooks import _offline, _run

from epistemic_monitor import authority, cli
from epistemic_monitor.bash_classifier import classify, script_write_paths
from epistemic_monitor.calibration import add_label, calibrate, load_calibration
from epistemic_monitor.gate import run_tier0
from epistemic_monitor.store import LedgerStore


def _auth_rule(store, scoped=False):
    kw = {"scope": {"paths": ["src/auth.py"]}} if scoped else {}
    return make_entry(store, type="constraint", statement="Never touch src/auth.py.",
                      falsifiers=[{"kind": "path_glob_forbidden", "glob": "src/auth.py"}], **kw)


def _bash(store, cwd, command):
    return run_tier0(store.active_entries(), "Bash", {"command": command}, str(cwd))


# ------------------------------------------------------------------ one-step Bash writes name their targets
ONE_STEP = [
    "sed -i 's/a/b/' src/auth.py",
    "sed -i.bak -e 's/a/b/' src/x.py src/auth.py && git status",
    "perl -pi -e 's/a/b/' src/auth.py",
    "black src/auth.py",
    "ruff check --fix src/auth.py",
    "git checkout -- src/auth.py",
    "git restore --staged src/auth.py",
    "patch src/auth.py < fix.diff",
    "python3 -c \"open('src/auth.py','w').write('')\"",
    "python3 -c \"from pathlib import Path; Path('src/auth.py').write_text('')\"",
    "python3 - <<'EOF'\nimport os\nos.remove('src/auth.py')\nEOF",
    "node -e \"require('fs').writeFileSync('src/auth.py','')\"",
    "rm ./src/auth.py",
    "rm $PWD/src/auth.py",
]


@pytest.mark.parametrize("command", ONE_STEP)
def test_one_step_bash_write_trips_path_falsifier(store, tmp_path, command):
    e = _auth_rule(store, scoped=True)
    r = _bash(store, tmp_path, command)
    assert e.id in r.scoped_entries, r.change.targets
    assert [t.kind for t in r.trips] == ["path_glob_forbidden"]


def test_absolute_path_under_cwd_trips(store):
    _auth_rule(store)
    r = _bash(store, "/home/u/proj", "rm /home/u/proj/src/auth.py && rm /home/u/proj/src/../src/auth.py")
    assert r.change.targets == ["src/auth.py"]
    assert [t.kind for t in r.trips] == ["path_glob_forbidden"]


@pytest.mark.parametrize("command", [
    "grep -n login src/auth.py",
    "python3 -c \"x = open('src/auth.py').read(); open('out/report.txt', 'w').write(x)\"",
    "git apply fix.diff",
    "sed -n 1,20p src/auth.py",
])
def test_reads_and_other_targets_do_not_trip(store, tmp_path, command):
    _auth_rule(store)
    assert _bash(store, tmp_path, command).trips == []


# ------------------------------------------------------------------ a shell script the command runs
@pytest.mark.parametrize("runner", ["bash fix.sh", "sh -e fix.sh", "./fix.sh", "source fix.sh", ". fix.sh",
                                    "cd . && bash ./fix.sh"])
def test_script_indirection_trips(store, tmp_path, runner):
    e = _auth_rule(store, scoped=True)
    (tmp_path / "fix.sh").write_text("#!/bin/sh\nset -e\nsed -i 's/old/new/' src/auth.py\n")
    r = _bash(store, tmp_path, runner)
    assert e.id in r.scoped_entries
    assert len(r.trips) == 1 and "written by a script" in r.trips[0].detail
    assert r.change.is_mutating


def test_script_in_tmp_by_absolute_path(store, tmp_path):
    _auth_rule(store)
    script = tmp_path / "elsewhere" / "fix.sh"
    script.parent.mkdir()
    script.write_text("rm src/auth.py\n")
    assert [t.kind for t in _bash(store, tmp_path, f"bash {script}").trips] == ["path_glob_forbidden"]


@pytest.mark.parametrize("body", [
    "grep -n login src/auth.py\ncat src/auth.py\n",       # mentions are not writes
    "# rm src/auth.py\n  # sed -i 's/a/b/' src/auth.py\necho ok\n",  # commented out
])
def test_script_mentions_and_comments_do_not_trip(store, tmp_path, body):
    _auth_rule(store)
    (tmp_path / "fix.sh").write_text(body)
    assert _bash(store, tmp_path, "bash fix.sh").trips == []


def test_missing_huge_and_binary_scripts_are_ignored(tmp_path):
    (tmp_path / "huge.sh").write_text("rm src/auth.py\n" + "#" * (70 * 1024))
    (tmp_path / "bin.sh").write_bytes(b"\0\0rm src/auth.py\n")
    assert script_write_paths("bash missing.sh; bash huge.sh; bash bin.sh", str(tmp_path)) == []


def test_chained_heredoc_still_trips(store, tmp_path):
    _auth_rule(store)
    r = _bash(store, tmp_path, "cat > fix.sh <<'EOF'\nrm src/auth.py\nEOF\nbash fix.sh")
    assert any(t.kind == "path_glob_forbidden" for t in r.trips)


def test_find_delete_is_a_write():
    assert classify("find src -name '*.pyc' -delete").verdict == "writes_source"


# ------------------------------------------------------------------ calibration inputs are the user's
CLI = 'python3 -I "/p/bin/em-hook" ledger'


@pytest.mark.parametrize("cmd", [f"{CLI} label tuA ok", f"{CLI} calibrate", f"{CLI} calibrate --min-labels 1",
                                 f"{CLI} label tuA conflict && {CLI} calibrate"])
def test_agent_cannot_label_or_calibrate(cmd):
    reason, _ = authority.check(cmd, "fix the parser bug")
    assert reason and "label" in reason


def test_user_typed_label_is_granted_once():
    user = "<command-name>/ledger</command-name>\n<command-args>label tuA ok</command-args>"
    reason, used = authority.check(f"{CLI} label tuA ok", user)
    assert reason is None and used == [("label", "tuA", "ok")]
    assert authority.check(f"{CLI} label tuA ok", user, used=set(used))[0]       # a grant is good for one run
    assert authority.check(f"{CLI} label tuB ok", user)[0]                        # and only for what was typed


@pytest.mark.parametrize("cmd", [f"{CLI} search label", f"{CLI} review", f"{CLI} list", f"{CLI} mark-good s1"])
def test_read_commands_and_mark_good_pass(cmd):
    assert authority.check(cmd, "fix the parser bug") == (None, [])


def _tx(tmp_path, text):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "isSidechain": False, "uuid": "u0",
                             "message": {"role": "user", "content": text}}) + "\n")
    return str(p)


def test_hook_denies_agent_label_and_passes_typed_one(cfg, store, monkeypatch, tmp_path):
    _offline(monkeypatch, dm=FakeDM(default=0.05))
    store.close()
    cmd = {"tool_name": "Bash", "tool_input": {"command": f"{CLI} label tuA ok"}, "tool_use_id": "tL"}
    out = _run("pre_tool_use", {**cmd, "transcript_path": _tx(tmp_path, "fix the parser bug")}, cfg)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    typed = "<command-name>/ledger</command-name>\n<command-args>label tuA ok</command-args>"
    out = _run("pre_tool_use", {**cmd, "tool_use_id": "tL2", "transcript_path": _tx(tmp_path, typed)}, cfg)
    assert not out or out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_one_call_labelled_twelve_times_is_one_sample(cfg):
    for _ in range(12):
        add_label(cfg, {"tool_use_id": "tuA", "label": "ok", "scores": {"C-0001": 0.85}, "types": {"C-0001": "constraint"}})
    cal = calibrate(cfg, [], min_labels=12)
    assert cal.tau_by_type == {}
    assert load_calibration(cfg).tau_by_type == {}


def test_relabel_keeps_the_last_label(cfg):
    for i in range(12):
        add_label(cfg, {"tool_use_id": f"t{i}", "label": "ok", "scores": {"C-0001": 0.4}, "types": {"C-0001": "constraint"}})
    add_label(cfg, {"tool_use_id": "t0", "label": "conflict", "scores": {"C-0001": 0.4}, "types": {"C-0001": "constraint"}})
    cal = calibrate(cfg, [], min_labels=12)
    assert cal.details["constraint"].get("tp", 0) + cal.details["constraint"].get("fn", 0) == 1


# ------------------------------------------------------------------ handoff marks a superseded rule
def test_handoff_marks_rule_superseded_while_subagent_ran(cfg, store, monkeypatch):
    _offline(monkeypatch, dm=FakeDM(default=0.05))
    make_entry(store, scope={"paths": ["src/db/*"]}, falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"\bpymysql\b"}])
    store.close()
    deny = {"tool_name": "Edit", "tool_use_id": "t1", "agent_id": "a1", "agent_type": "general-purpose",
            "tool_input": {"file_path": "src/db/client.py", "old_string": "a", "new_string": "import pymysql"}}
    assert _run("pre_tool_use", deny, cfg)["hookSpecificOutput"]["permissionDecision"] == "deny"
    s = LedgerStore(cfg)
    new = make_entry(s, statement="Use MySQL as the primary database.")
    assert s.supersede("D-0001", new.id, "switching", by="user")
    s.close()
    resp = {"agentId": "a1", "agentType": "general-purpose", "content": [{"type": "text", "text": "Done."}]}
    ctx = _run("post_tool_use_agent", {"tool_name": "Agent", "tool_response": resp}, cfg)["hookSpecificOutput"]["additionalContext"]
    assert "D-0001" in ctx and "no longer binding" in ctx and f"replaced by {new.id}" in ctx
