"""Hook handlers (DESIGN.md §4). Phase 0 scope: every event is wired and
logged; capture at PreCompact/SessionEnd; anchor audit and pinning at
PostCompact/SessionStart; Tier 0 gate in advisory mode; explicit
``SUPERSEDE <id>: <reason>`` lines honored under the authority rule.
Extraction (Phase 1) and gate Tiers 1–3 (Phase 2) are stubs that log.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import hooklib
from .config import Config
from .gate import format_citation, run_tier0
from .pinning import render_briefing, render_pinned
from .store import LedgerStore
from .transcript import capture_transcript, read_turns

_SUPERSEDE_RE = re.compile(r"\bSUPERSEDE\s+([DCPKR]-\d{4,})\s*:\s*([^\n]+)", re.IGNORECASE)


def _session(payload: dict[str, Any]) -> str:
    return str(payload.get("session_id", "") or "unknown")


def _apply_supersede_lines(text: str, by: str, store: LedgerStore, session_id: str) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for m in _SUPERSEDE_RE.finditer(text or ""):
        ok = store.supersede(m.group(1).upper(), None, m.group(2), by=by, session_id=session_id)
        applied.append({"entry": m.group(1).upper(), "by": by, "ok": ok})
        store.log_intervention({"kind": "supersede", "entry": m.group(1).upper(), "by": by, "ok": ok,
                                "reason": m.group(2), "session_id": session_id})
    return applied


# ---------------------------------------------------------------- session lifecycle
def session_start(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    source = str(payload.get("source", "startup"))
    active = store.active_entries()
    store.log_intervention({"kind": "session_start", "source": source, "active_entries": len(active),
                            "session_id": _session(payload)})
    if source == "compact" and cfg.pinned_path.exists():
        return hooklib.additional_context("session_start", cfg.pinned_path.read_text(encoding="utf-8"))
    if not active:
        return None
    orphaned = store.entries("orphaned")
    text = render_briefing(active, orphaned, cfg.get("pinning", "max_entries", 25), cfg.get("pinning", "max_chars", 10000))
    return hooklib.additional_context("session_start", text)


def session_end(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    # Capture only (DESIGN §11 decision 3): the 1.5 s default budget leaves no room for model calls.
    path = payload.get("transcript_path")
    label = f"{_session(payload)}-end-{int(time.time())}"
    out = capture_transcript(path, cfg.transcripts_dir, label) if path else None
    store.log_intervention({"kind": "capture", "event": "session_end", "reason": payload.get("reason"),
                            "file": str(out) if out else None, "session_id": _session(payload)})
    return None


# ---------------------------------------------------------------- compaction
def pre_compact(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    path = payload.get("transcript_path")
    record: dict[str, Any] = {"kind": "capture", "event": "pre_compact", "trigger": payload.get("trigger"),
                              "custom_instructions": bool(payload.get("custom_instructions")), "session_id": sid}
    if path and Path(path).exists():
        out = capture_transcript(path, cfg.transcripts_dir, f"{sid}-precompact-{int(time.time())}")
        turns, stats = read_turns(path, after_uuid=store.get_watermark(sid))
        record.update(file=str(out), lines=stats.lines, parsed=stats.parsed, errors=stats.errors,
                      new_turns=len(turns), compactions_seen=stats.compactions, versions=sorted(stats.versions))
        if stats.last_uuid:
            store.set_watermark(sid, stats.last_uuid)
        record["extraction"] = "deferred (Phase 1)"
    else:
        record["file"] = None
        record["note"] = "transcript_path missing"
    store.set_state(sid, "post_compaction_batches", cfg.get("audit", "behavior_watch_batches", 3))
    store.log_intervention(record)
    return None  # PreCompact cannot inject context; never block compaction.


def post_compact(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    """Anchor audit (DESIGN §7 step 1) and pin block (step 3). Entailment audit is Phase 1."""
    sid = _session(payload)
    summary = str(payload.get("compact_summary") or "")
    active = store.active_entries()
    dropped: set[str] = set()
    unaudited = 0
    for e in active:
        if not e.anchors:
            unaudited += 1
            continue
        if not any(a and a in summary for a in e.anchors):
            dropped.add(e.id)
    pinned = render_pinned(active, dropped, cfg.get("pinning", "max_entries", 25), cfg.get("pinning", "max_chars", 10000))
    cfg.pinned_path.write_text(pinned, encoding="utf-8")
    if cfg.get("pinning", "mirror_memory_md", True):
        _mirror_memory_md(cfg, pinned)
    audited = len(active) - unaudited
    store.log_intervention({"kind": "compaction_audit", "session_id": sid, "trigger": payload.get("trigger"),
                            "active": len(active), "audited": audited, "dropped": sorted(dropped),
                            "unaudited_no_anchors": unaudited,
                            "retention": (audited - len(dropped)) / audited if audited else None,
                            "summary_chars": len(summary), "entailment": "deferred (Phase 1)"})
    return None  # PostCompact cannot inject; SessionStart[compact] emits pinned.md.


def _mirror_memory_md(cfg: Config, pinned: str, max_lines: int = 40) -> None:
    """Maintain a bounded section in Claude Code's auto-memory index so the ledger
    survives even if hooks are disabled. Never creates MEMORY.md on its own."""
    path = cfg.memory_md_path
    if not path.exists():
        return
    start, end = "<!-- epistemic-monitor:start -->", "<!-- epistemic-monitor:end -->"
    section_lines = pinned.splitlines()[:max_lines]
    block = "\n".join([start, *section_lines, end])
    text = path.read_text(encoding="utf-8")
    if start in text and end in text:
        pre, rest = text.split(start, 1)
        _, post = rest.split(end, 1)
        text = pre + block + post
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- per turn
def user_prompt_submit(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    prompt = str(payload.get("prompt") or "")
    applied = _apply_supersede_lines(prompt, "user", store, sid)
    store.log_intervention({"kind": "prompt", "session_id": sid, "chars": len(prompt),
                            "supersedes": applied, "extraction": "deferred (Phase 1)"})
    return None


def pre_tool_use(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    tool = str(payload.get("tool_name") or "")
    if tool not in cfg.get("gate", "tools", []):
        return None
    entries = store.active_entries()
    result = run_tier0(entries, tool, payload.get("tool_input") or {}, str(cfg.cwd))
    enforce = bool(cfg.get("gate", "enforce", False))
    output: dict[str, Any] | None = None
    if result.trips:
        by_id = {e.id: e for e in entries}
        citation = format_citation(by_id, result.trips)
        user_backed = any(by_id[t.entry_id].provenance.source == "user" for t in result.trips if t.entry_id in by_id)
        denies = store.get_state(sid, "denies", 0)
        budget = int(cfg.get("gate", "deny_budget", 5))
        if enforce and user_backed and denies < budget:
            store.set_state(sid, "denies", denies + 1)
            result.decision, result.reason = "deny", citation
            output = hooklib.deny("pre_tool_use", citation)
        else:
            result.decision, result.reason = "advisory", citation
            output = hooklib.additional_context("pre_tool_use", citation)
    store.log_intervention({"kind": "gate", "session_id": sid, "tool_use_id": payload.get("tool_use_id"),
                            "enforce": enforce, "shadow_decision": "deny" if result.trips else "allow",
                            **result.to_dict()})
    return output


def post_tool_use(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    store.log_intervention({"kind": "post_tool_use", "session_id": _session(payload),
                            "tool": payload.get("tool_name"), "duration_ms": payload.get("duration_ms")})
    return None


def post_tool_batch(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    remaining = int(store.get_state(sid, "post_compaction_batches", 0) or 0)
    if remaining > 0:
        store.set_state(sid, "post_compaction_batches", remaining - 1)
    store.log_intervention({"kind": "batch", "session_id": sid, "tool_calls": len(payload.get("tool_calls") or []),
                            "behavior_watch_remaining": max(0, remaining - 1) if remaining else 0,
                            "behavior_watch": "deferred (Phase 1)"})
    return None


def stop(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    last = str(payload.get("last_assistant_message") or "")
    applied = _apply_supersede_lines(last, "agent", store, sid)
    store.log_intervention({"kind": "stop", "session_id": sid, "stop_hook_active": bool(payload.get("stop_hook_active")),
                            "chars": len(last), "supersedes": applied, "turn_check": "deferred (Phase 2)"})
    return None  # Phase 0 never blocks a Stop.


# ---------------------------------------------------------------- subagents
def subagent_start(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    active = store.active_entries()
    store.log_intervention({"kind": "subagent_start", "session_id": _session(payload),
                            "agent_type": payload.get("agent_type"), "active_entries": len(active)})
    if not active:
        return None
    return hooklib.additional_context("subagent_start", render_pinned(active, None, 15, 3000,
                                                                      title="Project decisions you must honor"))


def subagent_stop(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    store.log_intervention({"kind": "subagent_stop", "session_id": _session(payload),
                            "agent_id": payload.get("agent_id"), "check": "deferred (Phase 4)"})
    return None


HANDLERS: dict[str, hooklib.Handler] = {
    "session_start": session_start, "session_end": session_end,
    "user_prompt_submit": user_prompt_submit, "pre_tool_use": pre_tool_use,
    "post_tool_use": post_tool_use, "post_tool_batch": post_tool_batch, "stop": stop,
    "pre_compact": pre_compact, "post_compact": post_compact,
    "subagent_start": subagent_start, "subagent_stop": subagent_stop,
}
