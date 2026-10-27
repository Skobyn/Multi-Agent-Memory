"""Ledger entry schema (DESIGN.md §3)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

ENTRY_TYPES = ("decision", "constraint", "premise", "commitment", "process")
TYPE_PREFIX = {"decision": "D", "constraint": "C", "premise": "P", "commitment": "K", "process": "R"}
STATUSES = ("active", "superseded", "orphaned", "unknown_current", "retired")
SOURCES = ("user", "agent", "summary", "hook")
FALSIFIER_KINDS = (
    "diff_regex_forbidden",
    "diff_regex_required",
    "path_glob_forbidden",
    "path_glob_required_with",
    "command_regex_forbidden",
    "command_requires_prior",
    "shell_check",
)

_ID_RE = re.compile(r"^[DCPKR]-\d{4,}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Provenance:
    source: str = "agent"
    session_id: str = ""
    turn_uuid: str = ""
    timestamp: str = field(default_factory=now_iso)
    confirmed_by_user: bool = False
    extraction_confidence: float = 0.0


@dataclass
class Scope:
    paths: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


@dataclass
class Entry:
    id: str
    type: str
    statement: str
    rationale: str = ""
    scope: Scope = field(default_factory=Scope)
    falsifiers: list[dict[str, Any]] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)
    status: str = "active"
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    depends_on: list[str] = field(default_factory=list)
    valid_from: str = field(default_factory=now_iso)
    valid_to: str | None = None
    last_verified: str = field(default_factory=now_iso)
    embedding_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entry":
        d = dict(d)
        scope = d.get("scope") or {}
        prov = d.get("provenance") or {}
        return cls(
            id=d["id"], type=d["type"], statement=d["statement"], rationale=d.get("rationale", ""),
            scope=Scope(**{k: list(scope.get(k, [])) for k in ("paths", "tools", "topics")}),
            falsifiers=list(d.get("falsifiers", [])), anchors=list(d.get("anchors", [])),
            provenance=Provenance(**{k: prov[k] for k in prov if k in Provenance.__dataclass_fields__}),
            status=d.get("status", "active"), supersedes=list(d.get("supersedes", [])),
            superseded_by=d.get("superseded_by"), depends_on=list(d.get("depends_on", [])),
            valid_from=d.get("valid_from") or now_iso(), valid_to=d.get("valid_to"),
            last_verified=d.get("last_verified") or now_iso(), embedding_ref=d.get("embedding_ref"),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not _ID_RE.match(self.id):
            problems.append(f"bad id {self.id!r}")
        if self.type not in ENTRY_TYPES:
            problems.append(f"bad type {self.type!r}")
        elif not self.id.startswith(TYPE_PREFIX[self.type] + "-"):
            problems.append(f"id prefix does not match type {self.type}")
        if not self.statement.strip():
            problems.append("empty statement")
        if self.status not in STATUSES:
            problems.append(f"bad status {self.status!r}")
        if self.provenance.source not in SOURCES:
            problems.append(f"bad provenance.source {self.provenance.source!r}")
        for f in self.falsifiers:
            if not isinstance(f, dict) or f.get("kind") not in FALSIFIER_KINDS:
                problems.append(f"bad falsifier {f!r}")
        return problems


def make_id(entry_type: str, n: int) -> str:
    return f"{TYPE_PREFIX[entry_type]}-{n:04d}"
