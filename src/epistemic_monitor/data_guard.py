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
import shlex
from pathlib import Path
from typing import Any

from . import authority

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
STORE_FILES = ("ledger.jsonl", "index.db", "interventions.jsonl", "pinned.md", "calibration.json")
_DATA_DIR_RE = re.compile(r"\.claude/projects/[^\s'\"]*/epistemic\b")
_WRITE_CMDS = {"rm", "mv", "tee", "truncate", "dd", "shred", "unlink", "chmod", "chown", "touch"}
_COPY_CMDS = {"cp", "install", "ln", "rsync"}         # only the destination is written
_REMOVE_TREE = {"rm", "mv", "shred", "rsync", "find"}  # a write on an ancestor of the data dir takes it too
_SQL_READ_RE = re.compile(r"^\s*(?:select|with|explain|\.(?:tables|schema|dump|headers|mode|indexes|count))\b", re.IGNORECASE)
_SQL_WRITE_RE = re.compile(r"\b(?:update|delete|insert|replace|drop|alter|vacuum|attach|create|pragma)\b", re.IGNORECASE)
_REDIRECT_RE = re.compile(r"(?:\d|&)?>>?\|?\s*([^\s;&|<>]+)")
_PY_WRITE_RE = re.compile(r"\bopen\s*\([^)]*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+]|\.write_text\s*\(|\.write_bytes\s*\(|"
                          r"\bos\.(?:remove|unlink|replace|rename|truncate)\b|\bshutil\.|\.unlink\s*\(|"
                          r"\bsqlite3\.connect\b|\.execute\s*\(", re.IGNORECASE)
_STORE_IMPORT_RE = re.compile(r"epistemic_monitor\.(?:store|cli|hooks)\b|\bLedgerStore\b")
_STORE_MUTATOR_RE = re.compile(r"\.(?:retire|supersede|confirm|resolve|set_status|add_entry|update_entry|_apply_event|"
                               r"_append_event|_upsert|rebuild_from_events)\b")

REASON = ("Epistemic monitor: this call writes the plugin's own ledger data ({what}). The agent may not edit, delete "
          "or rewrite the ledger or its index: that would stop recorded decisions being enforced without a record. "
          "To change an entry, reply `SUPERSEDE <id>: <reason>` or ask the user to run `/ledger ...`. If the user "
          "wants the files changed by hand, they can run the command themselves.")


def _resolve(path: str, cwd: str | None) -> Path | None:
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_absolute():
        if not cwd:
            return None
        p = Path(cwd) / p
    return p.resolve()


def _under(path: str, root: Path, cwd: str | None) -> bool:
    """Does ``path`` name ``root`` or something inside it? A path that cannot be resolved (a NUL byte) counts as
    inside: this check fails closed."""
    if not path:
        return False
    try:
        p = _resolve(path, cwd)
        r = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return True
    return p is not None and (p == r or r in p.parents)


def _ancestor(path: str, root: Path, cwd: str | None) -> bool:
    try:
        p = _resolve(path, cwd)
        return p is not None and p in root.resolve().parents and p != Path(p.anchor)
    except (OSError, RuntimeError, ValueError):
        return False


def _mentions(text: str, root: Path) -> bool:
    return str(root) in text or str(root.resolve()) in text or bool(_DATA_DIR_RE.search(text))


def _tok(seg: str) -> list[str]:
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def _segment_reason(seg: str, root: Path, cwd: str | None) -> str | None:
    toks = _tok(seg)
    while toks and ("=" in toks[0] and not toks[0].startswith("-") or toks[0] in ("sudo", "command", "env", "nohup", "xargs")):
        toks = toks[1:]
    if not toks:
        return None
    head = os.path.basename(toks[0])
    hits = [t for t in toks[1:] if _under(t, root, cwd) or _mentions(t, root)]
    for target in _REDIRECT_RE.findall(seg):
        t = target.strip("'\"")
        if not t.startswith("&") and (_under(t, root, cwd) or _mentions(t, root)):
            return f"a redirect into {t}"
    if head in _REMOVE_TREE and any(_ancestor(t, root, cwd) for t in toks[1:]):
        if head != "find" or any(t in ("-delete", "-exec", "-execdir") for t in toks):
            return "a removal of a directory that holds the ledger data"
    if not hits:
        return None
    if head in _WRITE_CMDS:
        return f"`{head}` on the ledger data"
    if head in _COPY_CMDS and toks[-1] in hits:
        return f"`{head}` onto the ledger data"
    if head == "find" and any(t in ("-delete", "-exec", "-execdir", "-fprint") for t in toks):
        return "`find` acting on the ledger data"
    if head in ("sed", "perl", "gawk", "awk", "ruby") and any(
            t.startswith("-i") or t in ("--in-place", "inplace") or (t.startswith("-") and not t.startswith("--")
                                                                      and "i" in t and head in ("sed", "perl"))
            for t in toks[1:]):
        return "an in-place edit of the ledger data"
    if head.startswith("sqlite"):
        after = toks[toks.index(hits[0]) + 1:] if hits[0] in toks else []
        sql = " ".join(t for t in after if not t.startswith("-"))
        if not sql or _SQL_WRITE_RE.search(sql) or not _SQL_READ_RE.search(sql):
            return "a write (or SQL from stdin) against the ledger index"
    if head.startswith("python") and _PY_WRITE_RE.search(seg):
        return "a script that writes the ledger data"
    return None


def _bash_reason(command: str, root: Path, cwd: str | None) -> str | None:
    if not command.strip() or authority.only_ledger(command):
        return None  # the ledger CLI: the authority check gates it
    if _STORE_IMPORT_RE.search(command) and (_PY_WRITE_RE.search(command) or _STORE_MUTATOR_RE.search(command)):
        return "a script that drives the ledger store directly"
    if "<<" in command and _mentions(command, root) and (_PY_WRITE_RE.search(command) or _SQL_WRITE_RE.search(command)):
        return "a script (heredoc) that writes the ledger data"
    here = cwd
    for seg in authority.segments(command) or [command]:
        toks = _tok(seg)
        if toks and toks[0] in ("cd", "pushd"):
            target = toks[1] if len(toks) > 1 else "~"
            try:
                p = _resolve(target, here)
            except (OSError, RuntimeError, ValueError):
                p = None
            here = str(p) if p is not None else here
            continue
        why = _segment_reason(seg, root, here)
        if why:
            return why
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
