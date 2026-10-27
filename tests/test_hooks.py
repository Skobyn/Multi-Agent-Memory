import io
import json
import os
import subprocess
import sys

from helpers import FIXTURES, ROOT, make_entry

from epistemic_monitor import hooklib
from epistemic_monitor.hooks import HANDLERS
from epistemic_monitor.store import LedgerStore

OLD = FIXTURES / "transcript_synthetic_older_format.jsonl"


def _run(event, payload, cfg):
    out = io.StringIO()
    payload = {"session_id": "s1", "cwd": str(cfg.cwd), "hook_event_name": hooklib.HOOK_EVENT_NAMES[event], **payload}
    rc = hooklib.run(event, HANDLERS[event], payload=payload, stdout=out)
    assert rc == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def _logged(cfg, kind):
    s = LedgerStore(cfg)
    try:
        return [r for r in s.recent_interventions(200) if r.get("kind") == kind]
    finally:
        s.close()


def test_session_start_is_silent_without_entries(cfg):
    assert _run("session_start", {"source": "startup"}, cfg) is None
    assert _logged(cfg, "hook")[-1]["event"] == "session_start"


def test_session_start_briefs_and_pins(cfg, store):
    make_entry(store)
    store.close()
    out = _run("session_start", {"source": "startup"}, cfg)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart" and "D-0001" in ctx and "PostgreSQL" in ctx


def test_pre_compact_captures_and_sets_watermark(cfg):
    out = _run("pre_compact", {"trigger": "auto", "transcript_path": str(OLD), "custom_instructions": None}, cfg)
    assert out is None
    rec = _logged(cfg, "capture")[-1]
    assert rec["event"] == "pre_compact" and rec["compactions_seen"] == 1 and rec["versions"] == ["2.0.76"]
    assert os.path.exists(rec["file"])
    s = LedgerStore(cfg)
    try:
        assert s.get_watermark("s1") == "a-008"
    finally:
        s.close()


def test_post_compact_anchor_audit_pins_dropped_entries(cfg, store):
    make_entry(store, anchors=["PostgreSQL", "src/db/client.py"])
    make_entry(store, statement="Use Redis for caching.", anchors=["Redis"])
    store.close()
    summary = "Earlier: the user wanted PostgreSQL and we edited src/db/client.py. Nothing about caching."
    _run("post_compact", {"trigger": "auto", "compact_summary": summary}, cfg)
    rec = _logged(cfg, "compaction_audit")[-1]
    assert rec["dropped"] == ["D-0002"] and rec["retention"] == 0.5
    pinned = cfg.pinned_path.read_text()
    assert pinned.index("D-0002") < pinned.index("D-0001")  # dropped first
    assert "re-pinned" in pinned and "not overridable" in pinned
    out = _run("session_start", {"source": "compact"}, cfg)
    assert "D-0002" in out["hookSpecificOutput"]["additionalContext"]


def test_supersede_lines_respect_authority(cfg, store):
    make_entry(store)                      # D-0001 user
    make_entry(store, source="agent", confirmed=False, statement="Use pytest-xdist.")  # D-0002 agent
    store.close()
    _run("stop", {"last_assistant_message": "SUPERSEDE D-0001: Mongo is better\nSUPERSEDE D-0002: not needed"}, cfg)
    s = LedgerStore(cfg)
    try:
        assert s.get_entry("D-0001").status == "active"      # agent may not override the user
        assert s.get_entry("D-0002").status == "superseded"  # agent may retire its own
    finally:
        s.close()
    _run("user_prompt_submit", {"prompt": "ok fine. SUPERSEDE D-0001: switching to Mongo"}, cfg)
    s = LedgerStore(cfg)
    try:
        assert s.get_entry("D-0001").status == "superseded"
    finally:
        s.close()


def test_gate_advisory_then_enforced(cfg, store, monkeypatch):
    make_entry(store, scope={"paths": ["src/db/*"]},
               falsifiers=[{"kind": "diff_regex_forbidden", "pattern": r"\bpymongo\b"}])
    store.close()
    payload = {"tool_name": "Edit", "tool_use_id": "t1",
               "tool_input": {"file_path": "src/db/client.py", "old_string": "a", "new_string": "import pymongo"}}
    out = _run("pre_tool_use", payload, cfg)
    assert "additionalContext" in out["hookSpecificOutput"] and "D-0001" in out["hookSpecificOutput"]["additionalContext"]
    rec = _logged(cfg, "gate")[-1]
    assert rec["decision"] == "advisory" and rec["shadow_decision"] == "deny" and rec["enforce"] is False
    monkeypatch.setenv("EPISTEMIC_GATE_ENFORCE", "true")
    out = _run("pre_tool_use", payload, cfg)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _run("pre_tool_use", {"tool_name": "Read", "tool_input": {"file_path": "x"}}, cfg) is None


def test_handler_errors_never_fail_the_session(cfg, monkeypatch):
    def boom(payload, cfg, store):
        raise RuntimeError("kaboom")
    out = io.StringIO()
    assert hooklib.run("stop", boom, payload={"session_id": "s1", "cwd": str(cfg.cwd)}, stdout=out) == 0
    assert out.getvalue() == ""
    assert _logged(cfg, "hook")[-1]["ok"] is False


def test_dispatcher_subprocess_smoke(cfg):
    env = {**os.environ, "EPISTEMIC_DATA_DIR": str(cfg.data_dir)}
    for event, payload in [("session_start", {"source": "startup"}),
                           ("pre_compact", {"trigger": "manual", "transcript_path": str(OLD)}),
                           ("post_compact", {"trigger": "manual", "compact_summary": "nothing"}),
                           ("stop", {"last_assistant_message": "done"})]:
        p = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "em-hook"), "hook", event],
                           input=json.dumps({"session_id": "smoke", "cwd": str(cfg.cwd), **payload}),
                           capture_output=True, text=True, env=env, timeout=30)
        assert p.returncode == 0, p.stderr
    p = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "em-hook"), "status"],
                       capture_output=True, text=True, env=env, cwd=str(cfg.cwd) if cfg.cwd.exists() else None, timeout=30)
    assert p.returncode == 0 and '"version"' in p.stdout
    hooks = json.load(open(ROOT / "hooks" / "hooks.json"))["hooks"]
    assert {"SessionStart", "PreCompact", "PostCompact", "PreToolUse", "Stop", "SessionEnd"} <= set(hooks)
