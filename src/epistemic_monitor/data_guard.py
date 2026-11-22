"""Tier 0 guard for the plugin's own data directory.

The gate reads ``index.db`` and replays ``ledger.jsonl``. Before this guard, the agent could stop a user entry from
being enforced without any record: ``sqlite3 index.db "update entries set status='retired' ..."``, an Edit of
``ledger.jsonl``, or a forged ``status`` event followed by ``rm index.db`` (probe, RESEARCH-2026-11-22). The agent
has no legitimate reason to write there; every ledger change goes through the ``em-hook ledger`` CLI, which the
authority check already gates. So a write is denied outright, not scored.

State-Transfer Auditing (arXiv 2611.23346) argues the boundary between the agent and its persistent memory is where
tool-only logs miss violations. A hash chain over ``ledger.jsonl`` was considered and dropped: the agent runs as the
same OS user and can recompute it, and the plugin's own unlocked concurrent appends would raise false alarms.

Limits: a write hidden behind indirection (a script file, base64, a variable holding the path) is not seen. This
catches the realistic case, an agent "cleaning up" an entry it believes is stale, and fails closed on it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import authority

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
STORE_FILES = ("ledger.jsonl", "index.db", "interventions.jsonl", "pinned.md", "calibration.json")
_STORE_NAME_RE = re.compile(r"(?:ledger\.jsonl|index\.db(?:-wal|-shm|-journal)?|interventions\.jsonl|calibration\.json)\b")
_DATA_DIR_RE = re.compile(r"\.claude/projects/[^\s'\"]*/epistemic\b")
# commands that change or remove a file named anywhere in the call
_WRITE_CMD_RE = re.compile(r"(?:^|[\s;&|(`])(?:rm|mv|cp|tee|truncate|dd|shred|ln|install|rsync|unlink|chmod|chown|touch)\b")
_SED_INPLACE_RE = re.compile(r"\b(?:sed|perl)\s+(?:-[A-Za-z]*\s+)*-[A-Za-z]*i|\bsed\s+[^|;&]*--in-place")
_SQL_WRITE_RE = re.compile(r"\b(?:update|delete|insert|replace|drop|alter|vacuum|attach|create|pragma)\b", re.IGNORECASE)
_REDIRECT_RE = re.compile(r"(?:\d|&)?>>?\|?\s*([^\s;&|<>]+)")
_PY_WRITE_RE = re.compile(r"\bopen\s*\(|\.write_text\s*\(|\.write_bytes\s*\(|\bos\.(?:remove|unlink|replace|rename)\b|"
                          r"\bshutil\.|\.unlink\s*\(|sqlite3\.connect|\bconnect\s*\(", re.IGNORECASE)
_STORE_IMPORT_RE = re.compile(r"epistemic_monitor\.(?:store|cli|hooks)\b|\bLedgerStore\b")

REASON = ("Epistemic monitor: this call writes the plugin's own ledger data ({what}). The agent may not edit, delete "
          "or rewrite the ledger or its index: that would stop recorded decisions being enforced without a record. "
          "To change an entry, reply `SUPERSEDE <id>: <reason>` or ask the user to run `/ledger ...`. If the user "
          "wants the files changed by hand, they can run the command themselves.")


def _under(path: str, root: Path, cwd: str | None) -> bool:
    if not path:
        return False
    p = Path(os.path.expanduser(path))
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    try:
        p, r = p.resolve(), root.resolve()
    except (OSError, RuntimeError):
        return False
    return p == r or r in p.parents


def _names_store(text: str, root: Path) -> bool:
    return (str(root) in text or str(root.resolve()) in text or bool(_DATA_DIR_RE.search(text))
            or bool(_STORE_NAME_RE.search(text)))


def _bash_reason(command: str, root: Path, cwd: str | None) -> str | None:
    if not command.strip() or authority.only_ledger(command):
        return None  # the ledger CLI: the authority check gates it
    if _STORE_IMPORT_RE.search(command):
        if _PY_WRITE_RE.search(command) or re.search(r"\.(?:retire|supersede|confirm|resolve|set_status|add_entry|"
                                                     r"update_entry|_apply_event|_append_event|rebuild_from_events)\b",
                                                     command):
            return "a script that drives the ledger store directly"
    if not _names_store(command, root):
        return None
    for target in _REDIRECT_RE.findall(command):
        t = target.strip("'\"")
        if _names_store(t, root) or _under(t, root, cwd):
            return f"a redirect into {t}"
    for seg in authority.segments(command) or [command]:
        if not _names_store(seg, root):
            continue
        low = seg.strip()
        if _WRITE_CMD_RE.search(" " + low):
            return "a file command on the ledger data"
        if _SED_INPLACE_RE.search(low):
            return "an in-place edit of the ledger data"
        if re.search(r"\bsqlite3?\b", low) and _SQL_WRITE_RE.search(low):
            return "a write statement against the ledger index"
        if _PY_WRITE_RE.search(low):
            return "a script that writes the ledger data"
    return None


def check(tool: str, tool_input: Any, data_dir: Path, cwd: str | None = None) -> str | None:
    """The deny reason when this call would write the plugin's data directory, else None. Reads are allowed."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool in EDIT_TOOLS:
        path = str(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")
        if _under(path, data_dir, cwd):
            return REASON.format(what=f"{tool} of {path}")
        return None
    if tool == "Bash":
        what = _bash_reason(str(ti.get("command") or ""), data_dir, cwd)
        return REASON.format(what=what) if what else None
    return None


__all__ = ["check", "EDIT_TOOLS", "STORE_FILES"]
