"""Compaction audit tiers that need a model (DESIGN.md §7 steps 2 and 4)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .decision_model import DecisionModel, DecisionModelError
from .schema import Entry

ENTAILMENT_INSTRUCTIONS = ("Does the summary preserve this decision so that an agent that reads only the summary "
                           "would still know about it and honor it?")
ENTAILMENT_CRITERIA = {"true": "The summary states the decision, constraint, or requirement, or states it in different "
                               "words with the same effect.",
                       "false": "The summary omits it, contradicts it, or mentions the topic without the actual "
                                "decision, constraint, or requirement."}

STOP_INSTRUCTIONS = ("Does the assistant message state, decide, or rely on something that contradicts one of the "
                     "listed entries?")
STOP_CRITERIA = {"true": "The message announces a choice, plan, or claim that goes against a listed decision, "
                         "constraint, premise, commitment, or required process step.",
                 "false": "The message is consistent with every listed entry, unrelated to them, or explicitly "
                          "supersedes an entry with a SUPERSEDE line."}

WATCH_INSTRUCTIONS = "Do these tool calls, taken together, conflict with any of the listed decisions or constraints?"
WATCH_CRITERIA = {"true": "At least one tool call does something a listed entry rules out, skips a step a listed entry "
                          "requires, or works against a listed decision's intent.",
                  "false": "The tool calls are consistent with every listed entry or unrelated to them."}


def entailment_audit(entries: list[Entry], summary: str, dm: DecisionModel, threshold: float = 0.5,
                     max_workers: int = 8, time_budget_s: float = 10.0) -> dict[str, float | None]:
    """One decision-model call per entry, in parallel. Returns {entry_id: p_preserved}
    with ``None`` for entries that could not be audited within the budget."""
    results: dict[str, float | None] = {e.id: None for e in entries}
    if not entries or not summary.strip():
        return results
    state_summary = summary[-60000:]

    def one(e: Entry) -> tuple[str, float | None]:
        try:
            r = dm.noul(ENTAILMENT_INSTRUCTIONS,
                        {"summary": state_summary,
                         "decision": {"id": e.id, "type": e.type, "statement": e.statement, "rationale": e.rationale}},
                        ENTAILMENT_CRITERIA)
            return e.id, r.p_true
        except DecisionModelError:
            return e.id, None

    deadline = time.monotonic() + time_budget_s
    pool = ThreadPoolExecutor(max_workers=max_workers)
    futures = [pool.submit(one, e) for e in entries]
    try:
        for f in as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
            eid, p = f.result()
            results[eid] = p
    except Exception:  # noqa: BLE001 - futures.TimeoutError: leave the rest as None
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def dropped_by_entailment(scores: dict[str, float | None], threshold: float) -> set[str]:
    return {eid for eid, p in scores.items() if p is not None and p < threshold}


def behavior_watch(entries: list[Entry], tool_calls: list[dict[str, Any]], dm: DecisionModel,
                   max_entries: int = 25) -> tuple[float, int]:
    """Slipstream-style check of a post-compaction tool batch against the pinned entries.
    Returns (p_conflict, latency_ms)."""
    listed = [{"id": e.id, "type": e.type, "statement": e.statement} for e in entries[:max_entries]]
    calls = []
    for c in tool_calls[:12]:
        name = str(c.get("tool_name") or c.get("name") or "")
        inp = c.get("tool_input") or c.get("input") or {}
        brief = {k: (str(v)[:400]) for k, v in (inp.items() if isinstance(inp, dict) else [])
                 if k in ("command", "file_path", "new_string", "content", "pattern", "notebook_path")}
        calls.append({"tool": name, "input": brief})
    r = dm.noul(WATCH_INSTRUCTIONS, {"entries": listed, "tool_calls": calls}, WATCH_CRITERIA)
    return r.p_true, r.latency_ms
