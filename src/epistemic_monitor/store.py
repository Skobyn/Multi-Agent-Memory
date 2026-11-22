"""Ledger storage (DESIGN.md §2, §3).

Source of truth is the append-only event log ``ledger.jsonl``. ``index.db``
(SQLite + FTS5, optional vectors) is a rebuildable projection. Every gate
or audit decision goes to ``interventions.jsonl``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .schema import Entry, TYPE_PREFIX, has_negation, now_iso


# fields of a user-authority entry the plugin itself updates (``resolve(keep=True)``, drift rebaseline, embeddings)
_USER_ENTRY_UPDATABLE = frozenset({"status", "depends_on", "last_verified", "embedding_ref", "scope_fingerprint"})


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class LedgerStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.dirty = False  # set when this process records a ledger event (see hooklib.run)
        config.ensure_dirs()
        self._conn = sqlite3.connect(str(config.index_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        if self.count_entries() == 0 and config.ledger_path.exists():
            self.rebuild_from_events()

    # ------------------------------------------------------------------ schema
    def _create_tables(self) -> None:
        with self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL,
                    json TEXT NOT NULL, embedding BLOB, updated_at TEXT NOT NULL)"""
            )
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(id, statement, rationale)"
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS watermarks (
                    session_id TEXT PRIMARY KEY, last_uuid TEXT, updated_at TEXT NOT NULL)"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY (session_id, key))"""
            )

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ events
    def _append_event(self, event: dict[str, Any]) -> None:
        event = {"ts": now_iso(), **event}
        with open(self.config.ledger_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def iter_events(self) -> Iterable[dict[str, Any]]:
        if not self.config.ledger_path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self.config.ledger_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def rebuild_from_events(self) -> int:
        """Replay ledger.jsonl into index.db. Returns number of events applied."""
        with self._conn:
            self._conn.execute("DELETE FROM entries")
            self._conn.execute("DELETE FROM entries_fts")
            self._conn.execute("DELETE FROM counters")
        n = 0
        for ev in self.iter_events():
            self._apply_event(ev, record=False)
            n += 1
        return n

    def _weakens_user_entry(self, ev: dict[str, Any]) -> str | None:
        """The id of a user-authority entry this event would take out of enforcement (a non-active status, or user
        authority stripped) without ``by="user"``, else None. ``retire``/``resolve``/``supersede`` already refuse that
        at the API; this also applies on replay, so a line in ledger.jsonl with no ``by`` or an agent ``by``, followed
        by deleting index.db, cannot retire a user rule (RESEARCH-2026-11-22). A forged ``by: user`` line still
        replays: the file is the agent's to forge only if it can write it, which ``data_guard`` denies. Closing a
        user review item for good (retired/superseded) also needs the user; only a return to active is free. Only
        bookkeeping fields of a user entry may change without the user (``_USER_ENTRY_UPDATABLE``)."""
        kind = ev.get("event")
        if kind not in ("status", "entry_update", "entry_add", "supersede") or ev.get("by") == "user":
            return None
        eid = {"entry_add": (ev.get("entry") or {}).get("id"), "supersede": ev.get("old")}.get(kind, ev.get("id"))
        old = self.get_entry(str(eid)) if eid else None
        if old is None or not old.user_authority:
            return None
        if kind == "supersede":
            # store.supersede already refuses this live; a forged line must not do it on replay either
            return old.id if old.status not in ("superseded", "retired") else None
        if kind == "entry_add":
            return old.id  # ids are never reused (next_id): a second add would overwrite the user's entry wholesale
        if kind == "status":
            after = old.to_dict() | {"status": ev.get("status")}
        else:
            fields = dict(ev.get("fields") or {})
            if set(fields) - _USER_ENTRY_UPDATABLE:
                return old.id  # rewriting the statement, falsifiers, scope or provenance of a user entry
            after = old.to_dict() | fields
        try:
            new = Entry.from_dict(after)
        except Exception:  # noqa: BLE001 - a malformed event must not change a user entry either
            return old.id
        if new.status != old.status and new.status != "active" and (old.status == "active"
                                                                   or new.status in ("retired", "superseded")):
            return old.id  # out of enforcement, or a review item closed for good; only back to active is free
        if not new.user_authority:
            return old.id
        return None

    def _apply_event(self, ev: dict[str, Any], record: bool = True) -> bool:
        """Apply (and, with ``record``, append) one event. False when the user-entry rule refused it."""
        kind = ev.get("event")
        weakened = self._weakens_user_entry(ev)
        if weakened:
            self.log_intervention({"kind": "user_entry_write_refused", "entry": weakened, "event": kind,
                                   "replay": not record, "session_id": ev.get("session_id", "")})
            return False
        if record:
            self._append_event(ev)
            self.dirty = True  # ledger changed in this process: pinned.md and the MEMORY.md mirror are stale
        if kind == "entry_add":
            self._upsert(Entry.from_dict(ev["entry"]))
        elif kind == "entry_update":
            e = self.get_entry(ev["id"])
            if e is None:
                return True
            d = e.to_dict()
            d.update(ev.get("fields", {}))
            self._upsert(Entry.from_dict(d))
        elif kind == "supersede":
            old, new = self.get_entry(ev["old"]), self.get_entry(ev["new"]) if ev.get("new") else None
            if old is not None:
                d = old.to_dict()
                d.update(status="superseded", superseded_by=ev.get("new"), valid_to=ev.get("ts") or now_iso(),
                         revoked_by=ev.get("by"))
                self._upsert(Entry.from_dict(d))
            if new is not None and ev["old"] not in new.supersedes:
                d = new.to_dict()
                d["supersedes"] = list(new.supersedes) + [ev["old"]]
                self._upsert(Entry.from_dict(d))
            # propagated invalidation (DESIGN §3): explicit dependents -> orphaned; same-scope entries with no
            # explicit edge -> unknown_current (their premise may have moved; the user decides at the next briefing).
            # Only a user supersession may knock a user-authority entry out of enforcement: otherwise an agent could
            # strip a user constraint by superseding its own entry with an overlapping scope (MemoryDojo 2611.09721).
            user_ok = ev.get("by") == "user"
            orphaned, skipped = set(), []
            for dep in self.dependents_of(ev["old"]):
                if dep.status == "active" and dep.user_authority and not user_ok:
                    skipped.append(dep.id)
                elif dep.status == "active":
                    d = dep.to_dict()
                    d["status"] = "orphaned"
                    self._upsert(Entry.from_dict(d))
                    orphaned.add(dep.id)
            if old is not None and old.scope.paths:
                new_paths = set(new.scope.paths) if new is not None else set()
                for e in self.active_entries():
                    if e.id in orphaned or e.id == ev.get("new") or e.id == old.id or not e.scope.paths:
                        continue
                    shared = set(e.scope.paths) & set(old.scope.paths)
                    if shared and not (shared <= new_paths and new is not None):
                        if e.user_authority and not user_ok:
                            skipped.append(e.id)
                            continue
                        d = e.to_dict()
                        d["status"] = "unknown_current"
                        self._upsert(Entry.from_dict(d))
            if skipped and record:
                self.log_intervention({"kind": "cascade_skipped", "superseded": ev["old"], "entries": skipped,
                                       "by": ev.get("by"), "session_id": ev.get("session_id", "")})
        elif kind == "status":
            e = self.get_entry(ev["id"])
            if e is not None:
                d = e.to_dict()
                d["status"] = ev["status"]
                if ev["status"] == "retired":
                    d["revoked_by"] = ev.get("by")
                self._upsert(Entry.from_dict(d))
        return True

    # ------------------------------------------------------------------ entries
    def _upsert(self, entry: Entry, embedding: list[float] | None = None) -> None:
        with self._conn:
            row = self._conn.execute("SELECT embedding FROM entries WHERE id=?", (entry.id,)).fetchone()
            blob = _pack(embedding) if embedding else (row[0] if row else None)
            self._conn.execute(
                "INSERT OR REPLACE INTO entries (id, type, status, json, embedding, updated_at) VALUES (?,?,?,?,?,?)",
                (entry.id, entry.type, entry.status, json.dumps(entry.to_dict(), ensure_ascii=False), blob, now_iso()),
            )
            self._conn.execute("DELETE FROM entries_fts WHERE id=?", (entry.id,))
            self._conn.execute("INSERT INTO entries_fts (id, statement, rationale) VALUES (?,?,?)",
                               (entry.id, entry.statement, entry.rationale))

    def next_id(self, entry_type: str) -> str:
        with self._conn:
            row = self._conn.execute("SELECT value FROM counters WHERE name=?", (entry_type,)).fetchone()
            n = (row[0] if row else 0) + 1
            # never collide with ids already present (e.g. after a rebuild)
            while self.get_entry(f"{TYPE_PREFIX[entry_type]}-{n:04d}") is not None:
                n += 1
            self._conn.execute("INSERT OR REPLACE INTO counters (name, value) VALUES (?,?)", (entry_type, n))
        return f"{TYPE_PREFIX[entry_type]}-{n:04d}"

    def add_entry(self, entry: Entry, embedding: list[float] | None = None) -> Entry:
        problems = entry.validate()
        if problems:
            raise ValueError("; ".join(problems))
        if not self._apply_event({"event": "entry_add", "entry": entry.to_dict()}):
            raise ValueError(f"{entry.id} is a user entry; it cannot be replaced")
        if embedding:
            self._upsert(entry, embedding)
        return entry

    def update_entry(self, entry_id: str, **fields: Any) -> None:
        self._apply_event({"event": "entry_update", "id": entry_id, "fields": fields})

    def set_status(self, entry_id: str, status: str, reason: str = "") -> None:
        self._apply_event({"event": "status", "id": entry_id, "status": status, "reason": reason})

    def retire(self, entry_id: str, reason: str, by: str = "agent", session_id: str = "") -> bool:
        """Close an entry without a replacement. Authority rule as for supersede: a user-made entry needs by='user'."""
        e = self.get_entry(entry_id)
        if e is None or e.status == "retired":
            return False
        if e.user_authority and by != "user":
            self.log_intervention({"kind": "retire_refused", "entry": entry_id, "by": by, "reason": reason, "session_id": session_id})
            return False
        self._apply_event({"event": "status", "id": entry_id, "status": "retired", "reason": reason, "by": by, "session_id": session_id})
        return True

    def duplicate_groups(self, jaccard: float = 0.8) -> list[list[Entry]]:
        """Active entries whose statements are the same after normalization or near-paraphrases (word-set Jaccard
        at or above ``jaccard``), grouped, earliest id first."""
        import re as _re
        def norm(t: str) -> str:
            return _re.sub(r"[^a-z0-9 ]+", " ", t.lower()).strip()
        entries = sorted(self.active_entries(), key=lambda e: (e.type, e.id))
        groups: list[list[Entry]] = []
        taken: set[str] = set()
        for i, a in enumerate(entries):
            if a.id in taken:
                continue
            aw = set(norm(a.statement).split())
            group = [a]
            for b in entries[i + 1:]:
                if b.id in taken or b.type != a.type:
                    continue
                bw = set(norm(b.statement).split())
                same = norm(a.statement) == norm(b.statement)
                if has_negation(a.statement) != has_negation(b.statement):
                    continue  # "Use X" and "Never use X" overlap heavily; never merge across polarity
                if same or (len(aw) >= 4 and len(bw) >= 4 and len(aw & bw) / len(aw | bw) >= jaccard):
                    group.append(b)
                    taken.add(b.id)
            if len(group) > 1:
                taken.add(a.id)
                groups.append(group)
        return groups

    def supersede(self, old_id: str, new_id: str | None, reason: str, by: str, session_id: str = "") -> bool:
        """Apply the authority rule (DESIGN §3): a user-provenance entry can only be
        superseded with ``by="user"``. Returns False when refused."""
        old = self.get_entry(old_id)
        if old is None:
            return False
        if old.user_authority and by != "user":
            self.log_intervention({"kind": "supersede_refused", "entry": old_id, "by": by,
                                   "reason": reason, "session_id": session_id})
            return False
        self._apply_event({"event": "supersede", "old": old_id, "new": new_id, "reason": reason,
                           "by": by, "session_id": session_id})
        return True

    def set_embedding(self, entry_id: str, embedding: list[float]) -> None:
        e = self.get_entry(entry_id)
        if e is not None:
            self._upsert(e, embedding)

    def get_entry(self, entry_id: str) -> Entry | None:
        row = self._conn.execute("SELECT json FROM entries WHERE id=?", (entry_id,)).fetchone()
        return Entry.from_dict(json.loads(row[0])) if row else None

    def entries(self, status: str | None = "active") -> list[Entry]:
        if status is None:
            rows = self._conn.execute("SELECT json FROM entries ORDER BY id").fetchall()
        else:
            rows = self._conn.execute("SELECT json FROM entries WHERE status=? ORDER BY id", (status,)).fetchall()
        return [Entry.from_dict(json.loads(r[0])) for r in rows]

    def active_entries(self) -> list[Entry]:
        return self.entries("active")

    def count_entries(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def confirm(self, entry_id: str, by: str, session_id: str = "") -> bool:
        """Promote an active agent/summary entry to user authority (it then hard-denies like a user entry and is
        protected by the authority rule). User-only: the agent endorsing its own entry would be a self-confidence
        loop (DESIGN §8), so any other ``by`` is refused and logged. Never called automatically."""
        e = self.get_entry(entry_id)
        if e is None or e.status != "active" or e.user_authority:
            return False
        if by != "user":
            self.log_intervention({"kind": "confirm_refused", "entry": entry_id, "by": by, "session_id": session_id})
            return False
        d = e.provenance.__dict__ | {"confirmed_by_user": True}
        self._apply_event({"event": "entry_update", "id": e.id, "fields": {"provenance": d, "last_verified": now_iso()}})
        self.log_intervention({"kind": "entry_confirmed", "entry": e.id, "by": by, "session_id": session_id})
        return True

    def dependents_of(self, entry_id: str) -> list[Entry]:
        return [e for e in self.entries(None) if entry_id in e.depends_on]

    def revoked_by_user(self) -> list[Entry]:
        """Tombstones: superseded or retired entries that carried user authority or that the user revoked. Entries
        the agent revoked on its own are excluded, so the agent can re-make its own decisions."""
        return [e for e in self.entries("superseded") + self.entries("retired")
                if e.user_authority or e.revoked_by == "user"]

    def needs_review(self) -> list[Entry]:
        """Entries whose premise was superseded (orphaned) or may have moved (unknown_current)."""
        return self.entries("orphaned") + self.entries("unknown_current")

    def resolve(self, entry_id: str, keep: bool, session_id: str = "cli", by: str = "agent") -> bool:
        """Close a review item: keep it (back to active, dependency edges to superseded entries dropped) or retire it.
        Retiring a user-authority entry needs ``by="user"`` (authority rule, as for ``retire``)."""
        e = self.get_entry(entry_id)
        if e is None or e.status not in ("orphaned", "unknown_current"):
            return False
        if not keep and e.user_authority and by != "user":
            self.log_intervention({"kind": "resolve_refused", "entry": entry_id, "by": by, "session_id": session_id})
            return False
        if keep:
            live = [d for d in e.depends_on if (self.get_entry(d) or Entry(id=d, type="x", statement="")).status == "active"]
            self._apply_event({"event": "entry_update", "id": e.id, "fields": {"depends_on": live, "status": "active",
                                                                               "last_verified": now_iso()}})
        else:
            self._apply_event({"event": "status", "id": e.id, "status": "retired", "reason": "resolved at review",
                               "by": by, "session_id": session_id})
        self.log_intervention({"kind": "review_resolved", "entry": e.id, "keep": keep, "session_id": session_id})
        return True

    def graph(self) -> dict[str, Any]:
        """Dependency and supersession edges over every entry, for `ledger graph` and the panel."""
        nodes, edges = [], []
        for e in self.entries(None):
            nodes.append({"id": e.id, "type": e.type, "status": e.status, "statement": e.statement[:120],
                          "source": e.provenance.source})
            for d in e.depends_on:
                edges.append({"from": e.id, "to": d, "kind": "depends_on"})
            for o in e.supersedes:
                edges.append({"from": e.id, "to": o, "kind": "supersedes"})
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------------ search
    def search_keyword(self, query: str, top_k: int = 5) -> list[tuple[Entry, float]]:
        q = " OR ".join(t for t in "".join(c if c.isalnum() else " " for c in query).split() if len(t) > 2)
        if not q:
            return []
        rows = self._conn.execute(
            "SELECT e.json, f.rank FROM entries_fts f JOIN entries e ON e.id=f.id "
            "WHERE entries_fts MATCH ? AND e.status='active' ORDER BY rank LIMIT ?", (q, top_k)).fetchall()
        return [(Entry.from_dict(json.loads(r[0])), 1.0 / (1.0 + abs(r[1]))) for r in rows]

    def search_vector(self, embedding: list[float], top_k: int = 5) -> list[tuple[Entry, float]]:
        rows = self._conn.execute("SELECT json, embedding FROM entries WHERE status='active' AND embedding IS NOT NULL").fetchall()
        scored = [(Entry.from_dict(json.loads(j)), cosine(embedding, _unpack(b))) for j, b in rows]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------------ watermarks / state
    def get_watermark(self, session_id: str) -> str | None:
        row = self._conn.execute("SELECT last_uuid FROM watermarks WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row else None

    def set_watermark(self, session_id: str, last_uuid: str | None) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO watermarks (session_id, last_uuid, updated_at) VALUES (?,?,?)",
                               (session_id, last_uuid, now_iso()))

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM session_state WHERE session_id=? AND key=?", (session_id, key)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, session_id: str, key: str, value: Any) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO session_state (session_id, key, value, updated_at) VALUES (?,?,?,?)",
                               (session_id, key, json.dumps(value), now_iso()))

    def append_state(self, session_id: str, key: str, item: Any, cap: int) -> None:
        """Append to a list-valued state key in one write transaction, keeping the newest ``cap`` items."""
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT value FROM session_state WHERE session_id=? AND key=?", (session_id, key)).fetchone()
            items = list(json.loads(row[0]) or []) if row else []
            items = (items + [item])[-max(1, cap):]
            self._conn.execute("INSERT OR REPLACE INTO session_state (session_id, key, value, updated_at) VALUES (?,?,?,?)",
                               (session_id, key, json.dumps(items), now_iso()))

    def clear_state_if(self, session_id: str, key: str, expected: Any, value: Any = 0) -> None:
        """Compare-and-set: write ``value`` only if the key still holds ``expected`` (a run clears only its own marker)."""
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT value FROM session_state WHERE session_id=? AND key=?", (session_id, key)).fetchone()
            if row and json.loads(row[0]) == expected:
                self._conn.execute("UPDATE session_state SET value=?, updated_at=? WHERE session_id=? AND key=?",
                                   (json.dumps(value), now_iso(), session_id, key))

    def pop_state(self, session_id: str, key: str) -> list[Any]:
        """Read and clear a list-valued state key in one write transaction (two drains never get the same items)."""
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT value FROM session_state WHERE session_id=? AND key=?", (session_id, key)).fetchone()
            self._conn.execute("DELETE FROM session_state WHERE session_id=? AND key=?", (session_id, key))
        return list(json.loads(row[0]) or []) if row else []

    # ------------------------------------------------------------------ interventions
    def log_intervention(self, record: dict[str, Any]) -> None:
        record = {"ts": now_iso(), **record}
        with open(self.config.interventions_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def recent_interventions(self, n: int = 50) -> list[dict[str, Any]]:
        p = self.config.interventions_path
        if not p.exists():
            return []
        # read from the end: the log is append-only and never trimmed, so loading it whole grows without bound
        size = p.stat().st_size
        chunk = 64 * 1024
        with open(p, "rb") as fh:
            while True:
                start = max(0, size - chunk)
                fh.seek(start)
                data = fh.read(size - start)
                lines = data.splitlines()
                if start > 0:
                    lines = lines[1:]  # the first line may be cut
                if start == 0 or len(lines) >= n:
                    break
                chunk *= 4
        lines = [l.decode("utf-8", errors="replace") for l in lines[-n:]]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
