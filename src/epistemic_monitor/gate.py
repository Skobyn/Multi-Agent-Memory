"""Conformance gate, Tier 0 (DESIGN.md §8): deterministic falsifiers and the
Bash source-write classifier. Tiers 1–3 (retrieval, decision model, advisor)
arrive in Phase 2; the result structure already carries their slots so the
intervention log keeps one shape across phases.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bash_classifier import Classification, classify
from .schema import Entry

MUTATING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")


@dataclass
class Change:
    tool: str
    path: str = ""          # repo-relative when possible
    text: str = ""          # new content / new_string(s) / command
    command: str = ""
    is_new_file: bool = False


@dataclass
class Trip:
    entry_id: str
    kind: str
    detail: str

    def to_dict(self) -> dict:
        return {"entry_id": self.entry_id, "kind": self.kind, "detail": self.detail}


@dataclass
class GateResult:
    change: Change
    trips: list[Trip] = field(default_factory=list)
    classification: Classification | None = None
    scoped_entries: list[str] = field(default_factory=list)
    tier_reached: int = 0
    decision: str = "allow"   # allow | deny | advisory
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "tool": self.change.tool, "path": self.change.path, "is_new_file": self.change.is_new_file,
            "trips": [t.to_dict() for t in self.trips],
            "bash": self.classification.to_dict() if self.classification else None,
            "scoped_entries": self.scoped_entries, "tier_reached": self.tier_reached,
            "decision": self.decision, "reason": self.reason,
        }


def _rel(path: str, cwd: str) -> str:
    if not path:
        return ""
    try:
        return os.path.relpath(path, cwd) if os.path.isabs(path) else path
    except ValueError:
        return path


def change_from_tool_input(tool: str, tool_input: dict[str, Any], cwd: str) -> Change:
    ti = tool_input or {}
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        return Change(tool=tool, text=cmd, command=cmd)
    path = _rel(str(ti.get("file_path") or ti.get("notebook_path") or ""), cwd)
    if tool == "Write":
        abs_path = Path(cwd) / path if path and not os.path.isabs(path) else Path(path or "")
        return Change(tool=tool, path=path, text=str(ti.get("content", "")), is_new_file=bool(path) and not abs_path.exists())
    if tool == "Edit":
        return Change(tool=tool, path=path, text=str(ti.get("new_string", "")))
    if tool == "MultiEdit":
        edits = ti.get("edits") or []
        return Change(tool=tool, path=path, text="\n".join(str(e.get("new_string", "")) for e in edits if isinstance(e, dict)))
    if tool == "NotebookEdit":
        return Change(tool=tool, path=path, text=str(ti.get("new_source", "")))
    return Change(tool=tool, path=path, text="")


def entry_in_scope(entry: Entry, change: Change) -> bool:
    if entry.scope.tools and change.tool not in entry.scope.tools:
        return False
    if entry.scope.paths:
        targets = [change.path] if change.path else []
        if change.tool == "Bash":
            targets = classify(change.command).paths
        if not targets:
            return False
        return any(fnmatch.fnmatch(t, g) or fnmatch.fnmatch("./" + t, g) for t in targets for g in entry.scope.paths)
    return True


def evaluate_falsifiers(entry: Entry, change: Change, cwd: str) -> list[Trip]:
    trips: list[Trip] = []
    for f in entry.falsifiers:
        kind = f.get("kind")
        try:
            if kind == "diff_regex_forbidden" and change.text and re.search(f["pattern"], change.text, re.MULTILINE):
                trips.append(Trip(entry.id, kind, f"pattern {f['pattern']!r} found in change"))
            elif kind == "diff_regex_required" and change.tool == "Write" and change.is_new_file \
                    and not re.search(f["pattern"], change.text, re.MULTILINE):
                trips.append(Trip(entry.id, kind, f"required pattern {f['pattern']!r} missing from new file"))
            elif kind == "path_glob_forbidden":
                paths = [change.path] if change.path else (classify(change.command).paths if change.tool == "Bash" else [])
                for p in paths:
                    if fnmatch.fnmatch(p, f["glob"]) or fnmatch.fnmatch("./" + p, f["glob"]):
                        trips.append(Trip(entry.id, kind, f"path {p} matches forbidden glob {f['glob']}"))
            elif kind == "path_glob_required_with" and change.tool == "Write" and change.is_new_file and change.path \
                    and fnmatch.fnmatch(change.path, f["glob"]):
                stem = Path(change.path).stem
                companion = f["requires"].replace("{stem}", stem)
                if not (Path(cwd) / companion).exists():
                    trips.append(Trip(entry.id, kind, f"new file {change.path} requires {companion}, which does not exist"))
            elif kind == "command_regex_forbidden" and change.tool == "Bash" and re.search(f["pattern"], change.command):
                trips.append(Trip(entry.id, kind, f"command matches forbidden pattern {f['pattern']!r}"))
            # shell_check is Tier 3 only (slow); never evaluated here.
        except (re.error, KeyError) as e:
            trips.append(Trip(entry.id, "falsifier_error", f"{kind}: {e}"))
    return trips


def run_tier0(entries: list[Entry], tool: str, tool_input: dict[str, Any], cwd: str) -> GateResult:
    change = change_from_tool_input(tool, tool_input, cwd)
    result = GateResult(change=change)
    if tool == "Bash":
        result.classification = classify(change.command)
    for e in entries:
        if e.status != "active" or not entry_in_scope(e, change):
            continue
        result.scoped_entries.append(e.id)
        result.trips.extend(evaluate_falsifiers(e, change, cwd))
    result.tier_reached = 0
    return result


def format_citation(entries_by_id: dict[str, Entry], trips: list[Trip]) -> str:
    lines = ["Epistemic monitor: this change conflicts with recorded decisions."]
    for t in trips:
        e = entries_by_id.get(t.entry_id)
        if e is None:
            continue
        prov = f"{e.provenance.source} decision" + (", confirmed by user" if e.provenance.confirmed_by_user else "")
        lines.append(f"- {e.id} ({prov}): \"{e.statement}\" — {t.detail}")
    lines.append("Either revise the change, or supersede explicitly by replying with "
                 "`SUPERSEDE <id>: <reason>`.")
    return "\n".join(lines)
