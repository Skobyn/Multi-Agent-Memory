"""Gate coverage audit (docs/RESEARCH-2026-11-17.md, I2).

The plugin's enforcement exists only while PreToolUse actually runs. Claude Code has shipped a bug where every
plugin hook stops firing after a compaction, a plugin path that moves under a running session fails every hook,
and a hook killed at its timeout fails open. From inside a dead hook nothing can notice, so a later process does:
compare the tool calls a transcript records against the gate records in interventions.jsonl, joined on
tool_use_id. A gated tool call with a result but no gate record ran unchecked.

Read-only and local: one pass over the transcript and the interventions log. Reported by `ledger status`,
`ledger coverage` and `ledger doctor` for a human, since the model reading a context line cannot fix dead hooks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .config import claude_config_dir, project_slug
from .transcript import KIND_COMPACT_BOUNDARY, KIND_TOOL_RESULT, KIND_TOOL_USE, iter_turns


def gate_coverage(transcript_path: str | Path, records: Iterable[dict[str, Any]], tools: Iterable[str]) -> dict[str, Any]:
    tools = set(tools)
    gated_ids: set[str] = set()
    killed: set[str] = set()
    crashed: set[str] = set()
    for r in records:
        tid = r.get("tool_use_id")
        if not tid:
            continue
        if r.get("kind") == "gate":
            gated_ids.add(str(tid))
        elif r.get("kind") == "hook" and r.get("event") == "pre_tool_use" and r.get("ok") is False:
            (killed if r.get("killed") else crashed).add(str(tid))
    calls: list[tuple[str, str, str, bool]] = []  # (id, tool, ts, after_compaction)
    results: set[str] = set()
    compacted = False
    session_id = ""
    for t in iter_turns(transcript_path):  # main chain only: subagent calls live in their own transcripts
        session_id = session_id or t.session_id
        if t.kind == KIND_COMPACT_BOUNDARY:
            compacted = True
        elif t.kind == KIND_TOOL_USE and t.tool_name in tools and t.tool_use_id:
            calls.append((t.tool_use_id, t.tool_name, t.timestamp, compacted))
        elif t.kind == KIND_TOOL_RESULT and t.tool_use_id:
            results.add(t.tool_use_id)
    # a call with no result yet is still running (or the session is): not a gap
    done = [c for c in calls if c[0] in results]
    missing = [c for c in done if c[0] not in gated_ids]
    first = missing[0] if missing else None
    return {
        "transcript": str(transcript_path), "session_id": session_id,
        "gated_tool_calls": len(done), "missing": len(missing),
        "missing_after_compaction": sum(1 for c in missing if c[3]),
        "killed": sum(1 for c in missing if c[0] in killed),
        "crashed": sum(1 for c in missing if c[0] in crashed),
        "first_missed": {"tool_use_id": first[0], "tool": first[1], "ts": first[2],
                         "after_compaction": first[3]} if first else None,
        "missing_ids": [c[0] for c in missing[:20]],
    }


def transcripts_dir(cwd: str | Path) -> Path:
    return claude_config_dir() / "projects" / project_slug(cwd)


def latest_transcript(cwd: str | Path) -> Path | None:
    d = transcripts_dir(cwd)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def iter_records(path: str | Path) -> Iterable[dict[str, Any]]:
    """Stream interventions.jsonl without loading it whole."""
    p = Path(path)
    if not p.exists():
        return
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


def coverage_for(cfg, transcript: str | Path | None = None) -> dict[str, Any] | None:
    path = Path(transcript) if transcript else latest_transcript(cfg.cwd)
    if path is None or not path.exists():
        return None
    return gate_coverage(path, iter_records(cfg.interventions_path), cfg.get("gate", "tools", []))
