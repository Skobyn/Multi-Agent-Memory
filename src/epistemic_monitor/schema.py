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
_NEGATION_RE = re.compile(r"\b(?:not|never|no|cannot|without|stop|avoid|forbidden|prohibited|"
                          r"(?:do|does|is|are|must|should|ca|wo)n['’ ]?t|dont)\b", re.IGNORECASE)


def has_negation(text: str) -> bool:
    """Crude polarity of a statement. Word-overlap and embedding similarity cannot tell "Use X" from "Never use X",
    so dedup and tombstone matching only treat two statements as the same when this agrees. A wrong answer here
    costs one duplicate entry; ignoring polarity costs a swallowed reversal."""
    return bool(_NEGATION_RE.search(text))


_VERB = r"(?:skip|ignore|edit|push|delete|bypass|disable|force|merge|commit|deploy|override|change|modify|drop|omit)"
_PERMIT_RE = re.compile(
    r"\b(?:allow(?:ed)?|permitted|permission|authori[sz]ed|exempt(?:ed|ion)?|waived?|bypass(?:ed|ing)?|"
    r"approved (?:to|for|editing|pushing|skipping|deleting|merging)|(?:the )?user approved|"
    r"(?:is|are) (?:fine|ok(?:ay)?|acceptable|optional)|acceptable|optional|ok(?:ay)? (?:to|for)|fine to|free to|"
    r"safe to|may " + _VERB + r"|can (?:be )?(?:skip(?:ped)?|ignored?|bypass(?:ed)?|disabled?)|can \w+ without|"
    r"no need|not (?:required|necessary|needed)|no longer (?:required|needed)|"
    r"(?:do|does)(?:n['’]?t| not) (?:need|have) to|(?:do|does)(?:n['’]?t| not) require|skip(?:s|ped|ping)?)\b",
    re.IGNORECASE)
_PROHIBIT_RE = re.compile(
    r"\b(?:never|must not|mustn['’]?t|shall not|cannot|can['’]?t|may not|may only|forbidden|prohibited|avoid(?:ing)?|"
    r"no exceptions?|only|not (?:allowed|permitted|approved|authori[sz]ed|optional|ok(?:ay)?|fine)|must|always)\b|"
    r"(?<!n['’]t )(?<!not )\brequires?\b|\b(?:do not|don['’]?t)\b(?! (?:need|have|require) )", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"[;:.!?]\s|,\s*(?=but\b|except\b|though\b|unless\b|and\b)|\b(?:but|except|though|unless|whereas)\b",
                        re.IGNORECASE)


def is_permissive(text: str) -> bool:
    """Does any clause of the statement grant a permission, exception or approval ("skipping tests for docs/ is
    allowed", "the user approved force-pushing", "no need to run lint", "hotfixes may skip review but must be
    tested")? Within a clause, restrictive wording wins ("never skip tests", "requires approval", "no exceptions").
    Crude on purpose: it is consulted only for a claimed user entry whose quote was not found in the user's turns,
    where a false hit costs that entry its user authority (Weighing the Envelope, arXiv 2611.23844: memory wording
    that grants authorization erodes a verification rule in proportion to its dose)."""
    return any(_PERMIT_RE.search(c) and not _PROHIBIT_RE.search(c) for c in _CLAUSE_RE.split(text or "") if c)


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
    scope_fingerprint: dict[str, str] = field(default_factory=dict)  # path -> content hash when written (agent premises only)
    evidence: str = ""  # quote the extractor cited at write time (<= 500 chars); previously only in the intervention log
    evidence_unverified: bool = False  # source=user but the quote has no counterpart in the user turns; display tag only
    revoked_by: str | None = None  # who superseded or retired it ("user" | "agent"); set from the event

    @property
    def user_authority(self) -> bool:
        """True when the user made this entry or later confirmed it (``Store.confirm``): it then gets the
        user's enforcement force and can only be superseded or retired by the user."""
        return self.provenance.source == "user" or self.provenance.confirmed_by_user

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
            evidence=str(d.get("evidence", "")), evidence_unverified=bool(d.get("evidence_unverified", False)),
            scope_fingerprint={str(k): str(v) for k, v in (d.get("scope_fingerprint") or {}).items()},
            revoked_by=d.get("revoked_by"),
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
