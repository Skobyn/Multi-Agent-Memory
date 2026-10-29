"""Guards against the 2026-10-29 fan-out: child flag, child environment, slots, debounce."""
import io
import json
import os

from fakes import FakeDM, FakeLLM
from helpers import make_entry

from epistemic_monitor import hooklib, hooks, throttle
from epistemic_monitor.hooks import HANDLERS


def test_child_env_strips_plugin_dirs_and_sets_flag(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_DIRS", "/x:/y")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "parent")
    monkeypatch.setenv("HOME", "/home/me")
    env = throttle.child_env()
    assert "CLAUDE_CODE_PLUGIN_DIRS" not in env and "CLAUDE_SESSION_ID" not in env
    assert env[throttle.CHILD_FLAG] == "1" and env["HOME"] == "/home/me"
    assert throttle.is_child(env) and not throttle.is_child({})


def test_hook_in_child_session_exits_silently(cfg, monkeypatch):
    monkeypatch.setenv(throttle.CHILD_FLAG, "1")
    out = io.StringIO()
    rc = hooklib.run("user_prompt_submit", HANDLERS["user_prompt_submit"],
                     payload={"prompt": "Switch everything to MongoDB today.", "session_id": "nested"}, stdout=out)
    assert rc == 0 and out.getvalue() == ""
    assert not (cfg.data_dir / "interventions.jsonl").exists()


def test_slots_cap_concurrency(tmp_path):
    with throttle.slot("x", 2, tmp_path) as a:
        assert a is True
        with throttle.slot("x", 2, tmp_path) as b:
            assert b is True
            with throttle.slot("x", 2, tmp_path) as c:
                assert c is False     # both slots busy: the caller skips
    with throttle.slot("x", 2, tmp_path) as d:
        assert d is True              # released on exit


def test_prompt_extraction_is_debounced_and_skips_when_busy(cfg, store, monkeypatch, tmp_path):
    llm = FakeLLM([{"entries": [{"type": "constraint", "statement": "Never use MongoDB.", "rationale": "", "scope": {}, "anchors": []}], "supersedes": []}])
    monkeypatch.setattr(hooks, "make_llm", lambda section: llm)
    monkeypatch.setattr(hooks, "make_decision_model", lambda c, fb=None: FakeDM(default=0.1))
    monkeypatch.setenv("EPISTEMIC_EMBEDDINGS_PROVIDER", "none")
    monkeypatch.setattr(throttle, "lock_dir", lambda: tmp_path / "locks")
    store.close()
    out = io.StringIO()
    hooklib.run("user_prompt_submit", HANDLERS["user_prompt_submit"], payload={"prompt": "Rule: never use MongoDB in this service.", "session_id": "s1"}, stdout=out)
    hooklib.run("user_prompt_submit", HANDLERS["user_prompt_submit"], payload={"prompt": "Also: always run pytest before commits.", "session_id": "s1"}, stdout=out)
    from epistemic_monitor.store import LedgerStore
    s2 = LedgerStore(cfg)
    try:
        prompts = [r for r in s2.recent_interventions(50) if r.get("kind") == "prompt"]
        assert prompts[0]["extraction"].get("added") == ["C-0001"]
        assert prompts[1]["extraction"] == {"skipped": "debounced", "seconds_since_last": prompts[1]["extraction"]["seconds_since_last"]}
        assert len(llm.calls) == 1
    finally:
        s2.close()
    # with the slots all held elsewhere, extraction is skipped as busy rather than queued
    monkeypatch.setenv("EPISTEMIC_EXTRACTION_MIN_INTERVAL_S", "0")
    with throttle.slot("extraction", 2, tmp_path / "locks"), throttle.slot("extraction", 2, tmp_path / "locks"):
        hooklib.run("user_prompt_submit", HANDLERS["user_prompt_submit"], payload={"prompt": "Third rule: ISO dates only.", "session_id": "s1"}, stdout=out)
    s3 = LedgerStore(cfg)
    try:
        last = [r for r in s3.recent_interventions(50) if r.get("kind") == "prompt"][-1]
        assert last["extraction"] == {"skipped": "busy", "max_concurrent": 2} and len(llm.calls) == 1
    finally:
        s3.close()
