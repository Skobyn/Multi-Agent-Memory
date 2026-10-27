import io
import json
import os
import subprocess
import sys

from fakes import FakeDM, FakeLLM
from helpers import FIXTURES, ROOT, make_entry

from epistemic_monitor import hooklib, hooks
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
        return [r for r in s.recent_interventions(300) if r.get("kind") == kind]
    finally:
        s.close()


def _offline(monkeypatch, llm=None, dm=None):
    """Keep hooks off the network: fake LLM, fake decision model, no embeddings, no background processes."""
    monkeypatch.setattr(hooks, "make_llm", lambda section: llm or FakeLLM([]))
    monkeypatch.setattr(hooks, "make_decision_model", lambda cfg, fb=None: dm or FakeDM(default=0.9))
    monkeypatch.setenv("EPISTEMIC_EMBEDDINGS_PROVIDER", "none")
    spawned = []
    monkeypatch.setattr(hooks.background, "spawn", lambda cfg, args: spawned.append(args) or 4242)
    return spawned


def test_session_start_is_silent_without_entries(cfg):
    assert _run("session_start", {"source": "startup"}, cfg) is None
    assert _logged(cfg, "hook")[-1]["event"] == "session_start"


def test_session_start_briefs_and_pins(cfg, store):
    make_entry(store)
    store.close()
    out = _run("session_start", {"source": "startup"}, cfg)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart" and "D-0001" in ctx and "PostgreSQL" in ctx


def test_pre_compact_captures_extracts_and_sets_watermark(cfg, monkeypatch):
    llm = FakeLLM([{"entries": [{"type": "decision", "statement": "Use PostgreSQL as the primary database.", "source": "user",
                                 "evidence": "We decided on PostgreSQL", "turn_uuid": "u-001", "confidence": 0.9}]}])
    _offline(monkeypatch, llm=llm)
    out = _run("pre_compact", {"trigger": "auto", "transcript_path": str(OLD), "custom_instructions": None}, cfg)
    assert out is None
    rec = _logged(cfg, "capture")[-1]
    assert rec["event"] == "pre_compact" and rec["compactions_seen"] == 1 and rec["versions"] == ["2.0.76"]
    assert os.path.exists(rec["file"]) and rec["extraction"]["added"] == ["D-0001"]
    s = LedgerStore(cfg)
    try:
        assert s.get_watermark("s1") == "a-008" and s.get_entry("D-0001").provenance.source == "user"
    finally:
        s.close()
    assert "[user u-001]" in llm.calls[0][1] and "subagent chatter" not in llm.calls[0][1]


def test_post_compact_anchor_and_entailment_audit(cfg, store, monkeypatch):
    make_entry(store, anchors=["PostgreSQL", "src/db/client.py"])                       # D-0001 kept by anchors
    make_entry(store, statement="Use Redis for caching.", anchors=["Redis"])            # D-0002 anchor miss
    make_entry(store, statement="Never send email on the user's behalf.", type="constraint")  # C-0001 no anchors
    store.close()
    dm = FakeDM(by_id={"D-0001": 0.9, "D-0002": 0.9, "C-0001": 0.1})
    _offline(monkeypatch, dm=dm)
    summary = "Earlier: the user wanted PostgreSQL and we edited src/db/client.py. Nothing about caching or email."
    _run("post_compact", {"trigger": "auto", "compact_summary": summary}, cfg)
    rec = _logged(cfg, "compaction_audit")[-1]
    assert rec["dropped_by_anchor"] == ["D-0002"] and rec["dropped_by_entailment"] == ["C-0001"]
    assert rec["dropped"] == ["C-0001", "D-0002"] and rec["audited"] == 3 and rec["retention"] == 1 / 3
    pinned = cfg.pinned_path.read_text()
    assert pinned.index("C-0001") < pinned.index("D-0001") and pinned.index("D-0002") < pinned.index("D-0001")
    assert "re-pinned" in pinned and "not overridable" in pinned
    out = _run("session_start", {"source": "compact"}, cfg)
    assert "D-0002" in out["hookSpecificOutput"]["additionalContext"]


def test_post_compact_without_decision_model_still_pins(cfg, store, monkeypatch):
    make_entry(store, anchors=["Redis"])
    store.close()
    _offline(monkeypatch)
    monkeypatch.setenv("EPISTEMIC_AUDIT_ENTAILMENT_CHECK", "false")
    _run("post_compact", {"trigger": "manual", "compact_summary": "nothing relevant"}, cfg)
    rec = _logged(cfg, "compaction_audit")[-1]
    assert rec["dropped"] == ["D-0001"] and rec["entailment_scores"] == {}


def test_user_prompt_submit_extracts_incrementally(cfg, monkeypatch):
    llm = FakeLLM([{"entries": [{"type": "constraint", "statement": "Never add dependencies without asking.", "source": "user",
                                 "evidence": "don't add dependencies without asking", "confidence": 0.9}]}])
    _offline(monkeypatch, llm=llm)
    _run("user_prompt_submit", {"prompt": "Please don't add dependencies without asking me first.", "transcript_path": str(OLD)}, cfg)
    rec = _logged(cfg, "prompt")[-1]
    assert rec["extraction"]["added"] == ["C-0001"]
    # previous assistant message from the transcript tail is included as context
    assert "Switching the database layer to MongoDB" in llm.calls[0][1]
    s = LedgerStore(cfg)
    try:
        assert s.get_entry("C-0001").provenance.confirmed_by_user is True
    finally:
        s.close()


def test_supersede_lines_respect_authority(cfg, store, monkeypatch):
    _offline(monkeypatch)
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


def test_stop_and_session_end_spawn_background_extraction(cfg, monkeypatch):
    spawned = _offline(monkeypatch)
    _run("stop", {"last_assistant_message": "short"}, cfg)
    assert spawned == []
    _run("stop", {"last_assistant_message": "I decided to use Redis for caching. " * 10}, cfg)
    assert spawned and spawned[-1][0] == "extract-text" and "--role" in spawned[-1]
    _run("session_end", {"reason": "other", "transcript_path": str(OLD)}, cfg)
    assert spawned[-1] == ["extract-pending"]
    assert _logged(cfg, "capture")[-1]["background_pid"] == 4242
    markers = list((cfg.data_dir / "pending").glob("*.json"))
    assert len(markers) == 1 and json.loads(markers[0].read_text())["session_id"] == "s1"
    # the next session start retries whatever is still pending
    _run("session_start", {"source": "startup"}, cfg)
    assert spawned[-1] == ["extract-pending"] and _logged(cfg, "session_start")[-1]["pending_captures"] == 1


def test_extract_pending_clears_marker_only_on_success(cfg, monkeypatch):
    from epistemic_monitor import cli
    llm = FakeLLM([{"entries": [{"type": "decision", "statement": "Use PostgreSQL as the primary database.", "source": "user",
                                 "confidence": 0.9}]}])
    spawned = _offline(monkeypatch, llm=llm)
    _run("session_end", {"reason": "other", "transcript_path": str(OLD)}, cfg)
    marker = next((cfg.data_dir / "pending").glob("*.json"))
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    # first attempt: the LLM fails -> marker stays
    monkeypatch.setattr(hooks, "make_llm", lambda section: FakeLLM(["not json"]))
    assert cli.main(["extract-pending"]) == 0
    assert marker.exists()
    # second attempt succeeds -> marker removed, entry added, watermark set
    monkeypatch.setattr(hooks, "make_llm", lambda section: llm)
    assert cli.main(["extract-pending"]) == 0
    assert not marker.exists()
    s = LedgerStore(cfg)
    try:
        assert s.get_entry("D-0001") is not None and s.get_watermark("s1") == "a-008"
    finally:
        s.close()


def test_behavior_watch_reinjects_after_compaction(cfg, store, monkeypatch):
    make_entry(store)
    store.close()
    dm = FakeDM(default=0.85)
    _offline(monkeypatch, dm=dm)
    _run("pre_compact", {"trigger": "auto", "transcript_path": str(OLD)}, cfg)   # arms 3 batches
    _run("post_compact", {"trigger": "auto", "compact_summary": "x"}, cfg)
    calls = [{"tool_name": "Bash", "tool_input": {"command": "pip install pymongo"}}]
    out = _run("post_tool_batch", {"tool_calls": calls}, cfg)
    assert "conflict" in out["hookSpecificOutput"]["additionalContext"] and "D-0001" in out["hookSpecificOutput"]["additionalContext"]
    rec = _logged(cfg, "batch")[-1]
    assert rec["behavior_watch_p"] == 0.85 and rec["reinjected"] is True and rec["behavior_watch_remaining"] == 2
    dm.default = 0.1
    assert _run("post_tool_batch", {"tool_calls": calls}, cfg) is None
    _run("post_tool_batch", {"tool_calls": calls}, cfg)
    assert _run("post_tool_batch", {"tool_calls": calls}, cfg) is None    # budget of 3 exhausted
    assert len([c for c in dm.calls if "tool_calls" in c["state"]]) == 3


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


def test_handler_errors_never_fail_the_session(cfg):
    def boom(payload, cfg, store):
        raise RuntimeError("kaboom")
    out = io.StringIO()
    assert hooklib.run("stop", boom, payload={"session_id": "s1", "cwd": str(cfg.cwd)}, stdout=out) == 0
    assert out.getvalue() == ""
    assert _logged(cfg, "hook")[-1]["ok"] is False


def test_dispatcher_subprocess_smoke(cfg):
    env = {**os.environ, "EPISTEMIC_DATA_DIR": str(cfg.data_dir), "EPISTEMIC_EXTRACTION_INCREMENTAL": "false",
           "EPISTEMIC_EXTRACTION_ON_STOP": "false", "EPISTEMIC_EXTRACTION_BACKGROUND_ON_SESSION_END": "false",
           "EPISTEMIC_AUDIT_ENTAILMENT_CHECK": "false", "EPISTEMIC_EMBEDDINGS_PROVIDER": "none",
           "EPISTEMIC_EXTRACTOR_PROVIDER": "openrouter", "EPISTEMIC_EXTRACTOR_TIMEOUT_MS": "1"}
    for event, payload in [("session_start", {"source": "startup"}),
                           ("pre_compact", {"trigger": "manual", "transcript_path": str(OLD)}),
                           ("post_compact", {"trigger": "manual", "compact_summary": "nothing"}),
                           ("stop", {"last_assistant_message": "done"})]:
        p = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "em-hook"), "hook", event],
                           input=json.dumps({"session_id": "smoke", "cwd": str(cfg.cwd), **payload}),
                           capture_output=True, text=True, env=env, timeout=60)
        assert p.returncode == 0, p.stderr
    p = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "em-hook"), "ledger", "add", "--type", "decision",
                        "--statement", "Use PostgreSQL as the primary database.", "--user"],
                       capture_output=True, text=True, env=env, cwd=str(cfg.cwd), timeout=30)
    assert p.returncode == 0 and "D-0001" in p.stdout, p.stderr
    p = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "em-hook"), "ledger", "list"],
                       capture_output=True, text=True, env=env, cwd=str(cfg.cwd), timeout=30)
    assert "PostgreSQL" in p.stdout
    p = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "em-hook"), "ledger", "supersede", "D-0001", "--reason", "x"],
                       capture_output=True, text=True, env=env, cwd=str(cfg.cwd), timeout=30)
    assert "refused" in p.stdout
    hooks_json = json.load(open(ROOT / "hooks" / "hooks.json"))["hooks"]
    assert {"SessionStart", "PreCompact", "PostCompact", "PreToolUse", "Stop", "SessionEnd", "PostToolBatch"} <= set(hooks_json)
