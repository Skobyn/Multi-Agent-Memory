"""Hook handlers (DESIGN.md §4), Phase 1.

Capture at PreCompact/SessionEnd; extraction incrementally on each user turn,
in full at PreCompact, and in the background after SessionEnd and Stop;
PostCompact anchor + entailment audit writing the pin block; SessionStart
briefing and post-compaction re-injection; behavior watch on the first tool
batches after a compaction; Tier 0 gate in advisory mode; SUPERSEDE lines
under the authority rule. Gate Tiers 1–3 are Phase 2.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import background, hooklib
from .audit import behavior_watch, dropped_by_entailment, entailment_audit
from .config import Config
from .decision_model import make_decision_model
from .embeddings import make_embedder
from .extractor import Extractor
from .calibration import load_calibration
from .gate import GatePipeline
from .llm import make_llm
from .pinning import render_briefing, render_pinned
from .store import LedgerStore
from .transcript import KIND_ASSISTANT_TEXT, KIND_USER_TEXT, capture_transcript, read_turns, rows_to_turns

_SUPERSEDE_RE = re.compile(r"\bSUPERSEDE\s+([DCPKR]-\d{4,})\s*:\s*([^\n]+)", re.IGNORECASE)


# ---------------------------------------------------------------- helpers
def _session(payload: dict[str, Any]) -> str:
    return str(payload.get("session_id", "") or "unknown")


_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _apply_supersede_lines(text: str, by: str, store: LedgerStore, session_id: str) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    if by == "agent":
        # An agent showing the syntax in a code block (e.g. telling the user how to supersede) is not a supersession.
        text = _FENCE_RE.sub("", text or "")
    for m in _SUPERSEDE_RE.finditer(text or ""):
        ok = store.supersede(m.group(1).upper(), None, m.group(2).strip(), by=by, session_id=session_id)
        applied.append({"entry": m.group(1).upper(), "by": by, "ok": ok})
        store.log_intervention({"kind": "supersede", "entry": m.group(1).upper(), "by": by, "ok": ok,
                                "reason": m.group(2).strip(), "session_id": session_id})
    return applied


def _dm_for(cfg: Config, store: LedgerStore):
    try:
        return make_decision_model(cfg.section("decision_model"), cfg.section("extractor"))
    except Exception as e:  # noqa: BLE001
        store.log_intervention({"kind": "component_unavailable", "component": "decision_model", "error": str(e)[:200]})
        return None


def _extractor_for(cfg: Config, store: LedgerStore, time_budget_s: float) -> Extractor | None:
    try:
        llm = make_llm(cfg.section("extractor"))
    except Exception as e:  # noqa: BLE001
        store.log_intervention({"kind": "component_unavailable", "component": "extractor_llm", "error": str(e)[:200]})
        return None
    try:
        embedder = make_embedder(cfg.section("embeddings"))
    except Exception:  # noqa: BLE001
        embedder = None
    ex = cfg.section("extraction")
    return Extractor(store, llm, embedder, dedup_cosine=float(ex.get("dedup_cosine", 0.92)),
                     max_chars_per_call=int(ex.get("max_chars_per_call", 24000)), max_calls=int(ex.get("max_calls", 6)),
                     time_budget_s=time_budget_s, min_confidence=float(ex.get("min_confidence", 0.5)))


def _advisor_for(cfg: Config, store: LedgerStore):
    try:
        return make_llm(cfg.section("advisor"))
    except Exception as e:  # noqa: BLE001
        store.log_intervention({"kind": "component_unavailable", "component": "advisor_llm", "error": str(e)[:200]})
        return None


def _embedder_for(cfg: Config):
    try:
        return make_embedder(cfg.section("embeddings"))
    except Exception:  # noqa: BLE001
        return None


def _tail_turns(transcript_path: str | None, max_bytes: int = 400_000) -> list:
    """Parse only the tail of the transcript (it may be large and lagging)."""
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    turns = []
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        turns.extend(t for t in rows_to_turns(row) if not t.is_sidechain)
    return turns


def _trajectory_slice(transcript_path: str | None, max_turns: int) -> str:
    lines = []
    for t in _tail_turns(transcript_path)[-(max_turns * 3):]:
        if t.kind in (KIND_USER_TEXT, KIND_ASSISTANT_TEXT) and t.text.strip():
            lines.append(f"[{t.role}]: {t.text.strip()[:600]}")
        elif t.kind == "tool_use":
            brief = (t.tool_input or {}).get("file_path") or (t.tool_input or {}).get("command") or ""
            lines.append(f"[tool {t.tool_name}]: {str(brief)[:200]}")
    return "\n".join(lines[-max_turns:])


def _last_assistant_text(transcript_path: str | None, max_bytes: int = 400_000) -> str:
    """Last assistant text in the transcript, reading only the tail (the file may be large and lagging)."""
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    last = ""
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for t in rows_to_turns(row):
            if t.kind == KIND_ASSISTANT_TEXT and not t.is_sidechain and t.text.strip():
                last = t.text
    return last


def _mirror_memory_md(cfg: Config, pinned: str, max_lines: int = 40) -> None:
    """Maintain a bounded section in Claude Code's auto-memory index so the ledger
    survives even if hooks are disabled. Never creates MEMORY.md on its own."""
    path = cfg.memory_md_path
    if not path.exists():
        return
    start, end = "<!-- epistemic-monitor:start -->", "<!-- epistemic-monitor:end -->"
    block = "\n".join([start, *pinned.splitlines()[:max_lines], end])
    text = path.read_text(encoding="utf-8")
    if start in text and end in text:
        pre, rest = text.split(start, 1)
        _, post = rest.split(end, 1)
        text = pre + block + post
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


def write_pinned(cfg: Config, store: LedgerStore, dropped: set[str] | None = None) -> str:
    active = store.active_entries()
    pinned = render_pinned(active, dropped, cfg.get("pinning", "max_entries", 25), cfg.get("pinning", "max_chars", 10000))
    cfg.pinned_path.write_text(pinned, encoding="utf-8")
    if cfg.get("pinning", "mirror_memory_md", True):
        _mirror_memory_md(cfg, pinned)
    return pinned


# ---------------------------------------------------------------- session lifecycle
def session_start(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    source = str(payload.get("source", "startup"))
    active = store.active_entries()
    pending = [p for p in pending_dir(cfg).glob("*.json")] if source != "compact" else []
    pid = background.spawn(cfg, ["extract-pending"]) if pending else None
    store.log_intervention({"kind": "session_start", "source": source, "active_entries": len(active),
                            "pending_captures": len(pending), "background_pid": pid, "session_id": _session(payload)})
    if source == "compact" and cfg.pinned_path.exists():
        return hooklib.additional_context("session_start", cfg.pinned_path.read_text(encoding="utf-8"))
    if not active:
        return None
    orphaned = store.entries("orphaned")
    text = render_briefing(active, orphaned, cfg.get("pinning", "max_entries", 25), cfg.get("pinning", "max_chars", 10000))
    return hooklib.additional_context("session_start", text)


def pending_dir(cfg: Config) -> Path:
    d = cfg.data_dir / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mark_pending(cfg: Config, capture: Path, session_id: str) -> Path:
    """Record a capture whose extraction has not completed. The background worker
    removes the marker when done; the next SessionStart retries anything left."""
    marker = pending_dir(cfg) / (capture.name + ".json")
    marker.write_text(json.dumps({"capture": str(capture), "session_id": session_id, "created": time.time()}), encoding="utf-8")
    return marker


def session_end(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    # Capture only (DESIGN §11 decision 3); extraction of the tail runs detached, and is
    # retried at the next SessionStart if the detached worker never finishes (a `-p` session
    # or a cloud sandbox can take its network down with it).
    sid = _session(payload)
    path = payload.get("transcript_path")
    out = capture_transcript(path, cfg.transcripts_dir, f"{sid}-end-{int(time.time())}") if path else None
    pid = None
    if out is not None and out.exists() and cfg.get("extraction", "background_on_session_end", True):
        mark_pending(cfg, out, sid)
        pid = background.spawn(cfg, ["extract-pending"])
    store.log_intervention({"kind": "capture", "event": "session_end", "reason": payload.get("reason"),
                            "file": str(out) if out else None, "background_pid": pid, "session_id": sid})
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
        ex = _extractor_for(cfg, store, float(cfg.get("extraction", "full_time_budget_s", 40)))
        if ex is not None and turns:
            record["extraction"] = ex.run(turns, sid, mode="full").to_dict()
        if stats.last_uuid:
            store.set_watermark(sid, stats.last_uuid)
    else:
        record["file"] = None
        record["note"] = "transcript_path missing"
    store.set_state(sid, "post_compaction_batches", int(cfg.get("audit", "behavior_watch_batches", 3)))
    store.log_intervention(record)
    return None  # PreCompact cannot inject context; never block compaction.


def post_compact(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    """Anchor audit, entailment audit, and pin block (DESIGN §7 steps 1–3)."""
    sid = _session(payload)
    summary = str(payload.get("compact_summary") or "")
    active = store.active_entries()
    dropped_anchor: set[str] = set()
    no_anchor = 0
    for e in active:
        if not e.anchors:
            no_anchor += 1
            continue
        if not any(a and a in summary for a in e.anchors):
            dropped_anchor.add(e.id)
    scores: dict[str, float | None] = {}
    dropped_ent: set[str] = set()
    if active and summary and cfg.get("audit", "entailment_check", True):
        dm = _dm_for(cfg, store)
        if dm is not None:
            scores = entailment_audit(active, summary, dm, float(cfg.get("audit", "entailment_threshold", 0.5)),
                                      time_budget_s=float(cfg.get("audit", "entailment_time_budget_s", 10)))
            dropped_ent = dropped_by_entailment(scores, float(cfg.get("audit", "entailment_threshold", 0.5)))
    dropped = dropped_anchor | dropped_ent
    audited = {e.id for e in active if e.anchors or scores.get(e.id) is not None}
    pinned = write_pinned(cfg, store, dropped)
    store.log_intervention({
        "kind": "compaction_audit", "session_id": sid, "trigger": payload.get("trigger"),
        "active": len(active), "audited": len(audited), "dropped": sorted(dropped),
        "dropped_by_anchor": sorted(dropped_anchor), "dropped_by_entailment": sorted(dropped_ent),
        "entailment_scores": {k: (round(v, 3) if v is not None else None) for k, v in scores.items()},
        "unaudited": len(active) - len(audited),
        "retention": (len(audited) - len(dropped & audited)) / len(audited) if audited else None,
        "summary_chars": len(summary), "pinned_chars": len(pinned),
    })
    return None  # PostCompact cannot inject; SessionStart[compact] emits pinned.md.


# ---------------------------------------------------------------- per turn
def user_prompt_submit(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    prompt = str(payload.get("prompt") or "")
    applied = _apply_supersede_lines(prompt, "user", store, sid)
    record: dict[str, Any] = {"kind": "prompt", "session_id": sid, "chars": len(prompt), "supersedes": applied}
    if cfg.get("extraction", "incremental", True) and len(prompt.strip()) >= 12:
        ex = _extractor_for(cfg, store, float(cfg.get("extraction", "incremental_time_budget_s", 20)))
        if ex is not None:
            prev = _last_assistant_text(payload.get("transcript_path"))
            record["extraction"] = ex.run_incremental(prompt, prev, sid).to_dict()
    store.log_intervention(record)
    return None


def pre_tool_use(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    tool = str(payload.get("tool_name") or "")
    if tool not in cfg.get("gate", "tools", []):
        return None
    history = list(store.get_state(sid, "tool_history", []) or [])
    entries = store.active_entries()
    dm = _dm_for(cfg, store) if entries else None
    llm = _advisor_for(cfg, store) if entries else None
    embedder = _embedder_for(cfg) if entries else None
    pipeline = GatePipeline(store, cfg, dm=dm, llm=llm, embedder=embedder, calibration=load_calibration(cfg))
    last_advice = str(store.get_state(sid, "last_advice", "") or "")
    slice_text = _trajectory_slice(payload.get("transcript_path"), int(cfg.get("gate", "advisor_slice_turns", 12))) if entries else ""
    result = pipeline.evaluate(tool, payload.get("tool_input") or {}, sid, history, slice_text, last_advice)
    # record this call in the session's tool history (for structural signals and process-order falsifiers)
    ch = result.change
    history.append({"tool": tool, "path": ch.path, "command": ch.command[:300], "fingerprint": ch.fingerprint(),
                    "mutating": ch.is_mutating, "decision": result.decision})
    store.set_state(sid, "tool_history", history[-int(cfg.get("gate", "history_max", 60)):])
    if result.advisor and result.advisor.get("next_step"):
        store.set_state(sid, "last_advice", result.advisor["next_step"])
    output: dict[str, Any] | None = None
    if result.decision == "deny":
        output = hooklib.deny("pre_tool_use", result.reason)
    elif result.decision == "advisory":
        output = hooklib.additional_context("pre_tool_use", result.reason)
    elif result.structural or (result.advisor and result.first_mutating_write and result.advisor.get("next_step")):
        notes = [f"Epistemic monitor note: {x['detail']}" for x in result.structural]
        if result.advisor and result.first_mutating_write and result.advisor.get("verdict") == "consistent":
            notes.append("Epistemic monitor (first edit review): " + result.advisor["next_step"])
        if notes:
            output = hooklib.additional_context("pre_tool_use", "\n".join(notes))
    store.log_intervention({"kind": "gate", "session_id": sid, "tool_use_id": payload.get("tool_use_id"),
                            "enforce": bool(cfg.get("gate", "enforce", True)),
                            "shadow_decision": "deny" if result.conflicts else "allow", **result.to_dict()})
    return output


def post_tool_use(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    store.log_intervention({"kind": "post_tool_use", "session_id": _session(payload),
                            "tool": payload.get("tool_name"), "duration_ms": payload.get("duration_ms")})
    return None


def post_tool_batch(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    remaining = int(store.get_state(sid, "post_compaction_batches", 0) or 0)
    calls = payload.get("tool_calls") or []
    record: dict[str, Any] = {"kind": "batch", "session_id": sid, "tool_calls": len(calls),
                              "behavior_watch_remaining": max(0, remaining - 1) if remaining else 0}
    output: dict[str, Any] | None = None
    if remaining > 0:
        store.set_state(sid, "post_compaction_batches", remaining - 1)
        active = store.active_entries()
        if calls and active and cfg.get("audit", "behavior_watch", True):
            dm = _dm_for(cfg, store)
            if dm is not None:
                try:
                    p, ms = behavior_watch(active, calls, dm)
                    record.update(behavior_watch_p=round(p, 3), behavior_watch_ms=ms)
                    if p >= float(cfg.get("audit", "behavior_watch_threshold", 0.6)):
                        pinned = cfg.pinned_path.read_text(encoding="utf-8") if cfg.pinned_path.exists() else write_pinned(cfg, store)
                        output = hooklib.additional_context(
                            "post_tool_batch",
                            "Epistemic monitor: the last tool batch appears to conflict with decisions recorded before "
                            "the context was compacted. Re-read them before continuing:\n\n" + pinned)
                        record["reinjected"] = True
                except Exception as e:  # noqa: BLE001
                    record["behavior_watch_error"] = str(e)[:200]
    store.log_intervention(record)
    return output


def stop(payload: dict[str, Any], cfg: Config, store: LedgerStore) -> dict[str, Any] | None:
    sid = _session(payload)
    last = str(payload.get("last_assistant_message") or "")
    applied = _apply_supersede_lines(last, "agent", store, sid)
    record: dict[str, Any] = {"kind": "stop", "session_id": sid, "stop_hook_active": bool(payload.get("stop_hook_active")),
                              "chars": len(last), "supersedes": applied}
    output: dict[str, Any] | None = None
    if cfg.get("gate", "stop_check", True) and not payload.get("stop_hook_active") and len(last) >= 80 \
            and bool(cfg.get("gate", "enforce", True)):
        active = store.active_entries()
        if active:
            dm = _dm_for(cfg, store)
            if dm is not None:
                try:
                    from .audit import STOP_CRITERIA, STOP_INSTRUCTIONS
                    listed = [{"id": e.id, "type": e.type, "statement": e.statement} for e in active[:25]]
                    r = dm.noul(STOP_INSTRUCTIONS, {"entries": listed, "assistant_message": last[-12000:]}, STOP_CRITERIA)
                    record["turn_check_p"] = round(r.p_true, 3)
                    if r.p_true >= float(cfg.get("gate", "stop_threshold", 0.8)):
                        reason = ("Epistemic monitor: before finishing, reconcile this reply with the recorded decisions. "
                                  "It appears to state or rely on something that contradicts one of them. Either correct the reply, "
                                  "or supersede the entry explicitly with `SUPERSEDE <id>: <reason>`.\n" +
                                  "\n".join(f"- {e['id']}: {e['statement']}" for e in listed))
                        output = hooklib.block(reason)
                        record["blocked"] = True
                except Exception as e:  # noqa: BLE001
                    record["turn_check_error"] = str(e)[:200]
    if cfg.get("extraction", "on_stop", True) and len(last) >= int(cfg.get("extraction", "stop_min_chars", 200)):
        tmp = cfg.data_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        f = tmp / f"{sid}-stop-{int(time.time() * 1000)}.txt"
        f.write_text(last, encoding="utf-8")
        record["background_pid"] = background.spawn(cfg, ["extract-text", str(f), "--session", sid, "--role", "assistant"])
    store.log_intervention(record)
    return output  # at most one block per turn (stop_hook_active guard)


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
