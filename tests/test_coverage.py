"""Gate coverage audit and the hook-kill record (coverage.py, hooklib.py; docs/RESEARCH-2026-11-17.md I2)."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from epistemic_monitor import cli
from epistemic_monitor.coverage import gate_coverage
from epistemic_monitor.store import LedgerStore

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "transcript_claude_code_2.1.311.jsonl"
TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"]


def _use(uid, tid, tool="Bash", sidechain=False):
    return {"type": "assistant", "uuid": uid, "sessionId": "s1", "timestamp": f"t-{uid}", "isSidechain": sidechain,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": tool, "input": {"command": "ls"}}]}}


def _result(uid, tid):
    return {"type": "user", "uuid": uid, "sessionId": "s1",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_real_fixture_fully_gated_has_no_gap():
    ids = []
    for line in FIX.read_text().splitlines():
        row = json.loads(line)
        for b in (row.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in TOOLS and not row.get("isSidechain"):
                ids.append(b["id"])
    assert ids
    cov = gate_coverage(FIX, [{"kind": "gate", "tool_use_id": i} for i in ids], TOOLS)
    assert cov["missing"] == 0 and cov["gated_tool_calls"] > 0
    cov = gate_coverage(FIX, [], TOOLS)
    assert cov["missing"] == cov["gated_tool_calls"] > 0


def test_gap_after_compaction_and_exclusions(tmp_path):
    rows = [_use("u1", "t1"), _result("r1", "t1"),
            _use("u2", "t2", tool="Read"), _result("r2", "t2"),           # not a gated tool
            _use("u3", "t3", sidechain=True), _result("r3", "t3"),        # subagent call
            {"type": "system", "subtype": "compact_boundary", "uuid": "c1", "sessionId": "s1"},
            _use("u4", "t4", tool="Edit"), _result("r4", "t4"),
            _use("u5", "t5")]                                             # still running: no result
    path = _write(tmp_path / "s1.jsonl", rows)
    cov = gate_coverage(path, [{"kind": "gate", "tool_use_id": "t1"},
                               {"kind": "hook", "event": "pre_tool_use", "tool_use_id": "t4", "ok": False, "killed": True}], TOOLS)
    assert cov["gated_tool_calls"] == 2
    assert cov["missing"] == 1 and cov["missing_after_compaction"] == 1 and cov["killed"] == 1
    assert cov["first_missed"] == {"tool_use_id": "t4", "tool": "Edit", "ts": "t-u4", "after_compaction": True}


def test_cli_coverage_reports_and_exits_nonzero(cfg, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path / "s1.jsonl", [_use("u1", "t1"), _result("r1", "t1")])
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["ledger", "coverage", str(path)]) == 1
    assert "GAP: 1 of 1" in capsys.readouterr().out
    s = LedgerStore(cfg)
    s.log_intervention({"kind": "gate", "tool_use_id": "t1"})
    s.close()
    assert cli.main(["ledger", "coverage", str(path)]) == 0
    assert "ok: all 1" in capsys.readouterr().out


def test_hook_killed_at_timeout_leaves_a_record(cfg):
    script = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from epistemic_monitor import hooklib\n"
        "def slow(p, c, s):\n    print('started', flush=True); time.sleep(30)\n"
        "hooklib.run('pre_tool_use', slow, payload={'session_id': 's1', 'tool_use_id': 'tk', 'cwd': %r})\n"
    ) % (str(ROOT / "src"), str(cfg.cwd))
    env = {k: v for k, v in os.environ.items() if k not in ("EPISTEMIC_MONITOR_CHILD",)}
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env)
    assert proc.stdout.readline().strip() == "started"
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    deadline = time.time() + 5
    recs = []
    while time.time() < deadline and not recs:
        s = LedgerStore(cfg)
        recs = [r for r in s.recent_interventions(50) if r.get("kind") == "hook" and r.get("killed")]
        s.close()
    assert recs and recs[0]["tool_use_id"] == "tk" and recs[0]["ok"] is False and recs[0]["event"] == "pre_tool_use"
