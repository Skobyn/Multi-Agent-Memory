"""Advisory flag on tool calls that write into the monitor's own data directory (docs/RESEARCH-2026-11-21.md).

The ledger changes through `/ledger` commands the user types, SUPERSEDE lines and the extractor. A direct write to
ledger.jsonl or index.db (``sed -i``, ``python3 -c``, the store API, Write/Edit) skipped every authority rule and,
before this, drew no note at all. This is not tamper-proofing: the agent runs as the same user, and a hash chain or key
would be in its reach too (State-Transfer Auditing, arXiv 2611.23346, assumes a harness the agent cannot write). It
catches a confused or steered agent, and it leaves a ``data_dir_write`` record (``ledger interventions``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config import Config

PATH_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# Bash: the data dir by any spelling (absolute, ~/, $HOME/, or anything under ~/.claude/projects/, globs included), or
# the store API inside an inline Python program. Bare file names (index.db, pinned.md) are not markers: other projects
# have their own, and this repo's commit messages and test filters name them.
_PROJECTS_RE = re.compile(r"\.claude/projects(?:/|\b)")
_STORE_RE = re.compile(r"\b(?:LedgerStore|epistemic_monitor\.store)\b")
_INLINE_PY_RE = re.compile(r"\bpython[\d.]*\b[^|;&]*(?:\s-c\b|<<)")
_READ_ONLY = frozenset(("cat", "head", "tail", "less", "more", "grep", "rg", "egrep", "fgrep", "wc", "ls", "jq",
                        "file", "stat", "du", "diff", "sort", "uniq", "cut", "zcat", "echo", "printf", "true",
                        "sed", "find", "cd", "pwd"))
_READ_ONLY_GIT = frozenset(("log", "show", "diff", "status", "blame"))
# flags that make an otherwise read-only program write
_WRITE_FLAGS = {"sort": ("-o", "--output"), "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")}
_SEGMENT_SPLIT = re.compile(r"[|&;\n]+")
_HARMLESS_REDIRECT = re.compile(r"\d?>&\d|&?\d?>>?\s*/dev/null")


def _sed_in_place(words: list[str]) -> bool:
    return any(w == "--in-place" or w.startswith("--in-place=") or (w.startswith("-") and not w.startswith("--") and "i" in w)
               for w in words[1:])


def _read_only(command: str) -> bool:
    """Every segment is a read-only command and nothing is redirected into a file. Errs toward False."""
    command = _HARMLESS_REDIRECT.sub(" ", command)
    if ">" in command or "`" in command or "$(" in command:
        return False
    for seg in _SEGMENT_SPLIT.split(command):
        words = seg.split()
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog == "git":
            if len(words) < 2 or words[1] not in _READ_ONLY_GIT:
                return False
            continue
        if prog not in _READ_ONLY:
            return False
        if prog == "sed" and _sed_in_place(words):
            return False
        if any(w == f or w.startswith(f + "=") for f in _WRITE_FLAGS.get(prog, ()) for w in words[1:]):
            return False
    return True


def _dir_spellings(data_dir: Path) -> list[str]:
    out = [str(data_dir)]
    try:
        rel = data_dir.resolve().relative_to(Path.home().resolve())
        out += [f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"]
    except (ValueError, OSError):
        pass
    return out


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def touched(tool: str, tool_input: Any, cfg: Config) -> str | None:
    """What in the data dir this call may write (a path or the matched marker), or None."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool in PATH_TOOLS:
        raw = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if not raw:
            return None
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = Path(cfg.cwd) / p
        return str(p) if _under(p, cfg.data_dir) else None
    if tool == "Bash":
        command = str(ti.get("command") or "")
        hit = next((d for d in _dir_spellings(cfg.data_dir) if d in command), None)
        if hit is None and "EPISTEMIC_DATA_DIR" in command:
            hit = "EPISTEMIC_DATA_DIR"
        if hit is None and (m := _PROJECTS_RE.search(command)):
            hit = m.group(0)
        if hit is None and _INLINE_PY_RE.search(command) and (m := _STORE_RE.search(command)):
            hit = m.group(0)
        if hit is None or _read_only(command):
            return None
        return hit
    return None


def note(hit: str) -> str:
    return ("Epistemic monitor note: this call writes into the monitor's own ledger data (matched: "
            f"{hit[:200]}). The ledger changes only through `/ledger` commands the user types, `SUPERSEDE <id>: "
            "<reason>` lines, and the extractor; a direct edit to these files is not a recorded change and carries "
            "no user authority. If the user asked for this edit, say so in your reply; otherwise do not make it.")
