"""Ledger extractor (DESIGN.md §6).

Two cadences, one extractor: incremental on each user turn (current message
plus the previous assistant message), and a full pass over turns since the
watermark at PreCompact / after SessionEnd. The compaction summary is never
a source. Output is validated, deduplicated, anchored, and written to the
ledger under the authority rule.
"""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .anchors import extract_anchors
from .llm import LLMClient, LLMError
from .schema import ENTRY_TYPES, FALSIFIER_KINDS, Entry, Provenance, Scope, now_iso
from .store import LedgerStore
from .transcript import KIND_ASSISTANT_TEXT, KIND_TOOL_USE, KIND_USER_TEXT, Turn

SYSTEM_PROMPT = """You maintain a decision ledger for a long-running software agent session. Context compaction will later
destroy the conversation; the ledger is what survives. Extract ONLY durable, explicit items of these types:

- decision: a choice that was made (technology, design, approach). "Use PostgreSQL as the primary database."
- constraint: something that must or must not be done. "Never send email on the user's behalf; draft only."
- premise: a fact the work relies on that could later change. "Q3 data is excluded because of the migration."
- commitment: something the agent promised the user. "Report test results before every push."
- process: a required step or ordering. "Run pytest before every commit."

Persistence test: include an item only if it would still apply if the user asked an unrelated question several
turns later. Skip transient task details, intentions that were not decided, and anything already in the
existing ledger (listed below). If new content explicitly reverses or replaces an existing entry, emit a
"supersedes" record instead of a new entry (and a new entry only if a replacement decision was stated).

For each entry give:
- "type": exactly one of "decision", "constraint", "premise", "commitment", "process" (required).
- "statement": one imperative sentence, specific and self-contained.
- "rationale": short, or "".
- "source": "user" if the USER stated or explicitly confirmed it; otherwise "agent".
- "evidence": a short verbatim quote from the turn that establishes it.
- "turn_uuid": the uuid shown in brackets for that turn, or "".
- "scope": {"paths": [globs], "tools": [tool names], "topics": [words]} — only when the entry clearly applies to
  specific files or tools; otherwise empty lists.
- "falsifiers": machine checks, only when obviously correct. Kinds:
  {"kind":"diff_regex_forbidden","pattern":<python regex that must not appear in new code>}
  {"kind":"diff_regex_required","pattern":<regex new files must contain>}
  {"kind":"path_glob_forbidden","glob":<glob that must not be written>}
  {"kind":"path_glob_required_with","glob":<glob of new files>,"requires":<companion path with {stem}>}
  {"kind":"command_regex_forbidden","pattern":<regex a shell command must not match>}
- "depends_on": ids of existing ledger entries this one presupposes, or [].
- "confidence": 0..1 that this is a real, durable item.

Reply with ONLY a JSON object:
{"entries":[{"type":"decision","statement":"...","rationale":"...","source":"user","evidence":"...","turn_uuid":"...",
             "scope":{"paths":[],"tools":[],"topics":[]},"falsifiers":[],"depends_on":[],"confidence":0.9}],
 "supersedes":[{"old":"D-0001","reason":"...","by":"user|agent"}]}
At most 10 entries. Empty lists are fine. No prose."""


@dataclass
class ExtractionResult:
    added: list[Entry] = field(default_factory=list)
    superseded: list[dict[str, Any]] = field(default_factory=list)
    skipped_duplicates: int = 0
    rejected: int = 0
    calls: int = 0
    llm_latency_ms: int = 0
    input_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"added": [e.id for e in self.added], "superseded": self.superseded,
                "skipped_duplicates": self.skipped_duplicates, "rejected": self.rejected, "calls": self.calls,
                "llm_latency_ms": self.llm_latency_ms, "input_tokens": self.input_tokens, "errors": self.errors[:5]}


def format_turns(turns: list[Turn], max_chars: int) -> str:
    lines: list[str] = []
    for t in turns:
        if t.kind == KIND_USER_TEXT:
            lines.append(f"[user {t.uuid}]: {t.text.strip()}")
        elif t.kind == KIND_ASSISTANT_TEXT:
            lines.append(f"[assistant {t.uuid}]: {t.text.strip()}")
        elif t.kind == KIND_TOOL_USE:
            ti = t.tool_input or {}
            brief = ti.get("file_path") or ti.get("command") or ti.get("pattern") or ""
            lines.append(f"[assistant {t.uuid} used tool {t.tool_name}]: {str(brief)[:300]}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[-max_chars:]


def chunk_turns(turns: list[Turn], max_chars: int) -> list[list[Turn]]:
    chunks: list[list[Turn]] = []
    cur: list[Turn] = []
    size = 0
    for t in turns:
        n = len(t.text) + 80
        if cur and size + n > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _ledger_block(entries: list[Entry]) -> str:
    if not entries:
        return "(empty)"
    return "\n".join(f"- {e.id} [{e.type}, {e.provenance.source}]: {e.statement}" for e in entries[:80])


def validate_falsifiers(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in raw or []:
        if not isinstance(f, dict) or f.get("kind") not in FALSIFIER_KINDS:
            continue
        kind = f["kind"]
        try:
            if kind in ("diff_regex_forbidden", "diff_regex_required", "command_regex_forbidden"):
                pat = str(f.get("pattern", ""))
                if not pat or len(pat) > 300:
                    continue
                re.compile(pat)
                out.append({"kind": kind, "pattern": pat})
            elif kind == "path_glob_forbidden":
                g = str(f.get("glob", ""))
                if g:
                    fnmatch.translate(g)
                    out.append({"kind": kind, "glob": g})
            elif kind == "path_glob_required_with":
                g, r = str(f.get("glob", "")), str(f.get("requires", ""))
                if g and r and "{stem}" in r:
                    out.append({"kind": kind, "glob": g, "requires": r})
            elif kind == "shell_check":
                cmd = str(f.get("command", ""))
                if cmd:
                    out.append({"kind": kind, "command": cmd[:300]})
        except re.error:
            continue
    return out


class Extractor:
    def __init__(self, store: LedgerStore, llm: LLMClient, embedder=None, *, dedup_cosine: float = 0.92,
                 max_chars_per_call: int = 24000, max_calls: int = 6, time_budget_s: float = 45.0,
                 min_confidence: float = 0.5) -> None:
        self.store, self.llm, self.embedder = store, llm, embedder
        self.dedup_cosine, self.max_chars, self.max_calls = dedup_cosine, max_chars_per_call, max_calls
        self.time_budget_s, self.min_confidence = time_budget_s, min_confidence

    # ------------------------------------------------------------------ public
    def run(self, turns: list[Turn], session_id: str, mode: str = "full") -> ExtractionResult:
        result = ExtractionResult()
        turns = [t for t in turns if t.kind in (KIND_USER_TEXT, KIND_ASSISTANT_TEXT, KIND_TOOL_USE)]
        if not any(t.kind in (KIND_USER_TEXT, KIND_ASSISTANT_TEXT) and t.text.strip() for t in turns):
            return result
        has_user_turn = any(t.kind == KIND_USER_TEXT for t in turns)
        t_start = time.monotonic()
        for chunk in chunk_turns(turns, self.max_chars)[-self.max_calls:]:
            if time.monotonic() - t_start > self.time_budget_s:
                result.errors.append("time budget exhausted before all chunks were processed")
                break
            active = self.store.active_entries()
            user = (f"MODE: {mode}\n\nEXISTING LEDGER (do not restate; reference by id in depends_on/supersedes):\n"
                    f"{_ledger_block(active)}\n\nCONVERSATION TURNS:\n{format_turns(chunk, self.max_chars)}")
            try:
                data, comp = self.llm.complete_json(SYSTEM_PROMPT, user)
            except LLMError as e:
                result.errors.append(str(e)[:300])
                result.calls += 1
                continue
            result.calls += 1
            result.llm_latency_ms += comp.latency_ms
            result.input_tokens += comp.input_tokens
            self._apply(data, active, session_id, has_user_turn, result)
        return result

    def run_incremental(self, prompt_text: str, prev_assistant_text: str, session_id: str) -> ExtractionResult:
        turns: list[Turn] = []
        if prev_assistant_text.strip():
            turns.append(Turn(uuid="", kind=KIND_ASSISTANT_TEXT, role="assistant", text=prev_assistant_text[-6000:]))
        turns.append(Turn(uuid="", kind=KIND_USER_TEXT, role="user", text=prompt_text[-12000:]))
        return self.run(turns, session_id, mode="incremental")

    # ------------------------------------------------------------------ private
    def _apply(self, data: dict, active: list[Entry], session_id: str, has_user_turn: bool, result: ExtractionResult) -> None:
        known_ids = {e.id for e in self.store.entries(None)}
        normalized_active = {_normalize(e.statement): e for e in active}
        candidates = data.get("entries") if isinstance(data, dict) else None
        new_statements: list[str] = []
        for c in candidates or []:
            if not isinstance(c, dict):
                result.rejected += 1
                continue
            etype = str(c.get("type", "")).strip().lower()
            statement = str(c.get("statement", "")).strip()
            try:
                conf = float(c.get("confidence", 0.7))
            except (TypeError, ValueError):
                conf = 0.7
            if etype not in ENTRY_TYPES or len(statement) < 8 or conf < self.min_confidence:
                result.rejected += 1
                continue
            norm = _normalize(statement)
            if norm in normalized_active or norm in new_statements:
                result.skipped_duplicates += 1
                continue
            embedding = self._embed(statement)
            if embedding and self._near_duplicate(embedding):
                result.skipped_duplicates += 1
                continue
            source = "user" if str(c.get("source", "agent")).lower() == "user" and has_user_turn else "agent"
            scope_raw = c.get("scope") if isinstance(c.get("scope"), dict) else {}
            scope = Scope(paths=[str(p) for p in scope_raw.get("paths", []) if p][:10],
                          tools=[str(t) for t in scope_raw.get("tools", []) if t][:6],
                          topics=[str(t) for t in scope_raw.get("topics", []) if t][:10])
            depends = [d for d in (c.get("depends_on") or []) if isinstance(d, str) and d in known_ids]
            evidence = str(c.get("evidence", ""))[:500]
            rationale = str(c.get("rationale", ""))[:500]
            entry = Entry(
                id=self.store.next_id(etype), type=etype, statement=statement[:500], rationale=rationale,
                scope=scope, falsifiers=validate_falsifiers(c.get("falsifiers")),
                anchors=extract_anchors(statement, evidence, rationale, *scope.paths),
                provenance=Provenance(source=source, session_id=session_id, turn_uuid=str(c.get("turn_uuid", ""))[:64],
                                      timestamp=now_iso(), confirmed_by_user=(source == "user"), extraction_confidence=conf),
                depends_on=depends,
            )
            try:
                self.store.add_entry(entry, embedding)
            except ValueError as e:
                result.rejected += 1
                result.errors.append(f"{entry.id}: {e}")
                continue
            self.store.log_intervention({"kind": "extracted", "entry": entry.id, "type": etype, "source": source,
                                         "confidence": conf, "session_id": session_id, "evidence": evidence[:200]})
            result.added.append(entry)
            new_statements.append(norm)
            known_ids.add(entry.id)
        for s in (data.get("supersedes") if isinstance(data, dict) else None) or []:
            if not isinstance(s, dict) or not isinstance(s.get("old"), str):
                continue
            by = "user" if str(s.get("by", "agent")).lower() == "user" and has_user_turn else "agent"
            ok = self.store.supersede(s["old"].strip().upper(), None, str(s.get("reason", ""))[:300], by=by, session_id=session_id)
            result.superseded.append({"old": s["old"], "by": by, "ok": ok})

    def _embed(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            vec = self.embedder.embed([text])[0]
            return vec or None
        except Exception:  # noqa: BLE001 - embeddings are optional; one failure disables them for this run
            self.embedder = None
            return None

    def _near_duplicate(self, embedding: list[float]) -> bool:
        hits = self.store.search_vector(embedding, top_k=1)
        return bool(hits) and hits[0][1] >= self.dedup_cosine
