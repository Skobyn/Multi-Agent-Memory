"""Premise drift check (idea from MemTrace, arXiv 2610.04838): evidence is only reusable while the
repository state it was observed against still holds.

Narrow by design: only agent/summary *premises* carry a fingerprint, and premises the user made or later
confirmed (``Entry.user_authority``) are skipped, so user-authority entries and
constraints never go stale from file edits. The check runs at SessionStart only (never in the gate),
is time-capped, and only moves an entry to ``unknown_current`` for re-verification via
``/ledger resolve``; it never retires anything.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

_GLOB = set("*?[")
_MAX_BYTES = 2_000_000


def _hash(root: Path, rel: str) -> str:
    p = (root / rel)
    try:
        if not p.resolve().is_relative_to(root.resolve()):
            return ""
        if not p.is_file():
            return "missing"
        if p.stat().st_size > _MAX_BYTES:
            return ""
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def fingerprint(root: Path, paths: list[str], max_paths: int = 10) -> dict[str, str]:
    """Content hashes for literal (non-glob) paths; unreadable or oversized files are skipped."""
    out: dict[str, str] = {}
    for rel in paths[:max_paths]:
        if not rel or _GLOB & set(rel):
            continue
        h = _hash(Path(root), rel)
        if h:
            out[rel] = h
    return out


def drift_check(store, root: Path, budget_s: float = 0.2, session_id: str = "") -> list[str]:
    """Mark active fingerprinted premises whose files changed as unknown_current. Returns their ids."""
    deadline = time.monotonic() + budget_s
    moved: list[str] = []
    for e in store.active_entries():
        if not e.scope_fingerprint or e.type != "premise" or e.user_authority:
            continue
        if time.monotonic() > deadline:
            break
        changed = [p for p, h in e.scope_fingerprint.items() if _hash(Path(root), p) not in ("", h)]
        if changed:
            store._apply_event({"event": "status", "id": e.id, "status": "unknown_current",
                                "reason": "file_drift", "session_id": session_id})
            store.log_intervention({"kind": "file_drift", "entry": e.id, "paths": changed, "session_id": session_id})
            moved.append(e.id)
    return moved


def rebaseline(store, root: Path, entry_id: str) -> None:
    e = store.get_entry(entry_id)
    if e is not None and e.scope_fingerprint:
        store._apply_event({"event": "entry_update", "id": e.id,
                            "fields": {"scope_fingerprint": fingerprint(Path(root), list(e.scope_fingerprint))}})
