"""Advisory flag on tool calls that write into the monitor's own data directory (docs/RESEARCH-2026-11-21.md).

The ledger changes through `/ledger` commands the user types, SUPERSEDE lines and the extractor. A direct write to
ledger.jsonl or index.db (``sed -i``, ``python3 -c``, the store API, Write/Edit) skipped every authority rule and,
before this, drew no note at all. This is not tamper-proofing: the agent runs as the same user, and a hash chain or key
would be in its reach too (State-Transfer Auditing, arXiv 2611.23346, assumes a harness the agent cannot write). It
catches a confused or steered agent, and it leaves a record that `/ledger audit` can count.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config import Config

PATH_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# substrings of a Bash command that name the data dir or the store; plain substrings, because pathlib and store-API
# writes do not look like writes to bash_classifier. "epistemic/" covers the ~ and $HOME spellings of the directory.
_MARKERS = ("epistemic/", "ledger.jsonl", "index.db", "interventions.jsonl", "pinned.md", "LedgerStore",
            "epistemic_monitor.store", "EPISTEMIC_DATA_DIR")
_READ_ONLY = frozenset(("cat", "head", "tail", "less", "more", "grep", "rg", "egrep", "fgrep", "wc", "ls", "jq",
                        "file", "stat", "du", "diff", "sort", "uniq", "cut", "zcat", "echo", "printf", "true"))
_SEGMENT_SPLIT = re.compile(r"\|\|?|&&|;|\n")
_HARMLESS_REDIRECT = re.compile(r"\d?>&\d|\d?>\s*/dev/null")


def _read_only(command: str) -> bool:
    """Every segment is a read-only command and nothing is redirected into a file. Errs toward False."""
    if ">" in _HARMLESS_REDIRECT.sub("", command) or "`" in command or "$(" in command:
        return False
    for seg in _SEGMENT_SPLIT.split(command):
        words = seg.split()
        if not words:
            continue
        if os.path.basename(words[0]) not in _READ_ONLY or any(w in ("-i", "--in-place", "-exec", "-delete") for w in words):
            return False
    return True


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
        data_dir = str(cfg.data_dir)
        hit = data_dir if data_dir in command else next((m for m in _MARKERS if m in command), None)
        if hit is None or _read_only(command):
            return None
        return hit
    return None


def note(hit: str) -> str:
    return ("Epistemic monitor note: this call writes into the monitor's own ledger data (matched: "
            f"{hit[:200]}). The ledger changes only through `/ledger` commands the user types, `SUPERSEDE <id>: "
            "<reason>` lines, and the extractor; a direct edit to these files is not a recorded change and carries "
            "no user authority. If the user asked for this edit, say so in your reply; otherwise do not make it.")
