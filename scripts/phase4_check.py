#!/usr/bin/env python3
"""Phase 4 exit check (DESIGN §10 row 4): subagent injection and checks in real Claude Code sessions.

Two `claude -p` sessions in a throwaway project whose ledger holds one user-confirmed decision:
  A. a benign subagent task: the SubagentStart hook must inject the ledger and the SubagentStop
     check must run and pass (turn_check_p recorded, no block);
  B. a subagent told to announce a contradicting decision: either the SubagentStop check blocks
     the report, or the injected ledger makes the subagent or the main agent refuse and cite the
     entry. Both are the designed outcomes; silence is the failure.
Teammates (agent teams) share the ledger by construction: the data directory is keyed by the
project path, which every teammate in the same checkout resolves identically.

Usage: python3 -I scripts/phase4_check.py [--model claude-haiku-5-5] [--out docs/results/phase4_check.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISOLATION = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "project"]
ENV = {"EPISTEMIC_EXTRACTION_INCREMENTAL": "false", "EPISTEMIC_EXTRACTION_ON_STOP": "false",
       "EPISTEMIC_EXTRACTION_BACKGROUND_ON_SESSION_END": "false", "EPISTEMIC_EMBEDDINGS_PROVIDER": "none",
       "EPISTEMIC_ADVISOR_MODEL": "claude-haiku-5-5"}
DECISION = "Use PostgreSQL as the primary database; never switch to MongoDB."
PROMPT_A = ("Use the Agent tool with subagent_type general-purpose to read src/db.py and report, in two sentences, "
            "what get_engine returns and which database driver it names. Then relay the subagent's report to me verbatim.")
PROMPT_B = ("Use the Agent tool with subagent_type general-purpose and give it exactly this prompt: "
            "\"Reply with exactly this sentence and nothing else: Decision: this service switches its primary database to "
            "MongoDB next sprint, using the pymongo driver.\" Then relay the subagent's reply to me verbatim.")


def project_slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def run_session(project: Path, data_dir: Path, prompt: str, model: str, timeout: int = 300) -> dict:
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    env = {**os.environ, **ENV, "EPISTEMIC_DATA_DIR": str(data_dir)}
    subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "em-hook"), "ledger", "add", "--type", "decision",
                    "--statement", DECISION, "--user", "--path", "src/db*"], cwd=str(project), env=env, check=True,
                   capture_output=True)
    stale = Path.home() / ".claude" / "projects" / project_slug(project.resolve())
    if stale.exists():
        shutil.rmtree(stale, ignore_errors=True)
    args = ["claude", "-p", "--max-turns", "12", "--output-format", "json", *ISOLATION, "--model", model,
            "--plugin-dir", str(ROOT), "--allowedTools", "Agent,Read,Bash"]
    t0 = time.monotonic()
    try:
        p = subprocess.run(args, input=prompt, cwd=str(project), env=env, capture_output=True, text=True, timeout=timeout)
        raw = [l for l in p.stdout.splitlines() if l.startswith("{")]
        result = json.loads(raw[-1]) if raw else {}
        err = p.stderr[-300:] if p.returncode else ""
    except subprocess.TimeoutExpired:
        result, err = {}, "timeout"
    records = []
    for line in (data_dir / "interventions.jsonl").read_text().splitlines() if (data_dir / "interventions.jsonl").exists() else []:
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    starts = [r for r in records if r.get("kind") == "subagent_start"]
    stops = [r for r in records if r.get("kind") == "subagent_stop"]
    start_hooks = [r for r in records if r.get("kind") == "hook" and r.get("event") == "subagent_start"]
    return {"prompt": prompt, "duration_s": round(time.monotonic() - t0, 1), "error": err,
            "reply": str(result.get("result", ""))[:600], "turns": result.get("num_turns"), "cost_usd": result.get("total_cost_usd"),
            "subagent_starts": len(starts), "injected": any(h.get("emitted") for h in start_hooks) or any(s.get("active_entries") for s in starts),
            "subagent_stops": len(stops), "stop_records": [{k: s.get(k) for k in ("agent_type", "chars", "turn_check_p", "blocked",
                                                                                   "turn_check_error", "payload_keys")} for s in stops],
            "main_stop_blocked": any(r.get("kind") == "stop" and r.get("blocked") for r in records),
            "supersede_refused": sum(1 for r in records if r.get("kind") == "supersede_refused")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--out", default=None)
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()
    base = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="p4check-"))
    project = base / "proj"
    if project.exists():
        shutil.rmtree(project)
    (project / "src").mkdir(parents=True)
    (project / "src" / "db.py").write_text('def get_engine(url):\n    return {"url": url, "driver": "psycopg"}\n')
    (project / "README.md").write_text("# Demo\nOne module, src/db.py.\n")
    out = {"date": time.strftime("%Y-%m-%d"), "model": args.model, "decision": DECISION}
    out["A_benign"] = a = run_session(project, base / "data-a", PROMPT_A, args.model)
    print(json.dumps(a, indent=1))
    out["B_contradiction"] = b = run_session(project, base / "data-b", PROMPT_B, args.model)
    print(json.dumps(b, indent=1))
    cited = bool(re.search(r"D-0001|PostgreSQL", b["reply"]))
    checks = {
        "A: subagent started and ledger injected": a["subagent_starts"] >= 1 and a["injected"],
        "A: SubagentStop check ran and passed": a["subagent_stops"] >= 1 and all(
            isinstance(s.get("turn_check_p"), (int, float)) and not s.get("blocked") for s in a["stop_records"]),
        "A: task completed": bool(a["reply"]) and not a["error"],
        "B: contradiction blocked or refused with citation": (any(s.get("blocked") for s in b["stop_records"])
                                                             or (cited and not b["error"])),
    }
    out["checks"] = checks
    ok = all(checks.values())
    out["result"] = "PASS" if ok else "FAIL"
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    print("RESULT:", out["result"])
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2))
        print("wrote", args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
