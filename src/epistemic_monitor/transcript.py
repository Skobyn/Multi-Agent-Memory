"""Claude Code transcript reader (DESIGN.md §5).

The JSONL format under ``~/.claude/projects/<slug>/<session>.jsonl`` is
undocumented and changes between versions, so this reader is defensive: an
unparseable line or an unknown row shape becomes a ``Turn`` of kind
``"other"`` (or is counted as an error) and never raises.

Verified against Claude Code 2.1.311 rows and the community-documented
compaction rows (``type=system, subtype=compact_boundary`` followed by a
``user`` row with ``isCompactSummary=true``).
"""

from __future__ import annotations

import gzip
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

KIND_USER_TEXT = "user_text"
KIND_ASSISTANT_TEXT = "assistant_text"
KIND_TOOL_USE = "tool_use"
KIND_TOOL_RESULT = "tool_result"
KIND_COMPACT_BOUNDARY = "compact_boundary"
KIND_COMPACT_SUMMARY = "compact_summary"
KIND_OTHER = "other"


@dataclass
class Turn:
    uuid: str
    kind: str
    role: str = ""  # user | assistant | system | ""
    text: str = ""
    parent_uuid: str | None = None
    timestamp: str = ""
    session_id: str = ""
    version: str = ""
    raw_type: str = ""
    tool_name: str = ""
    tool_use_id: str = ""
    tool_input: dict = field(default_factory=dict)
    is_sidechain: bool = False
    is_meta: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_message(self) -> bool:
        return self.kind in (KIND_USER_TEXT, KIND_ASSISTANT_TEXT)


@dataclass
class ReadStats:
    lines: int = 0
    parsed: int = 0
    errors: int = 0
    skipped_sidechain: int = 0
    compactions: int = 0
    last_uuid: str | None = None
    versions: set[str] = field(default_factory=set)


def _text_of_content(content) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
    return "\n".join(p for p in parts if p)


def rows_to_turns(row: dict) -> list[Turn]:
    """Convert one JSONL row into zero or more Turns.

    An assistant row with several content blocks yields one Turn per block
    (text, tool_use); a user row with tool_result blocks yields one Turn per
    result. Compaction rows yield a single boundary/summary Turn.
    """
    rtype = str(row.get("type", ""))
    base = dict(
        uuid=str(row.get("uuid") or row.get("leafUuid") or ""),
        parent_uuid=row.get("parentUuid"),
        timestamp=str(row.get("timestamp", "")),
        session_id=str(row.get("sessionId", "")),
        version=str(row.get("version", "")),
        raw_type=rtype,
        is_sidechain=bool(row.get("isSidechain", False)),
        is_meta=bool(row.get("isMeta", False)),
    )

    if rtype == "system" and row.get("subtype") == "compact_boundary":
        meta = row.get("compactMetadata") or {}
        return [Turn(kind=KIND_COMPACT_BOUNDARY, role="system",
                     extra={"trigger": meta.get("trigger"), "pre_tokens": meta.get("preTokens"),
                            "logical_parent": row.get("logicalParentUuid")}, **base)]

    if rtype == "summary":  # older format: standalone summary row
        return [Turn(kind=KIND_COMPACT_SUMMARY, role="system", text=str(row.get("summary", "")), **base)]

    message = row.get("message")
    if rtype in ("user", "assistant") and isinstance(message, dict):
        content = message.get("content")
        if rtype == "user" and row.get("isCompactSummary"):
            return [Turn(kind=KIND_COMPACT_SUMMARY, role="user", text=_text_of_content(content), **base)]
        turns: list[Turn] = []
        if isinstance(content, str):
            kind = KIND_USER_TEXT if rtype == "user" else KIND_ASSISTANT_TEXT
            turns.append(Turn(kind=kind, role=rtype, text=content, **base))
            return turns
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(str(block.get("text", "")))
                elif btype == "tool_use":
                    turns.append(Turn(kind=KIND_TOOL_USE, role=rtype,
                                      tool_name=str(block.get("name", "")),
                                      tool_use_id=str(block.get("id", "")),
                                      tool_input=block.get("input") if isinstance(block.get("input"), dict) else {},
                                      **base))
                elif btype == "tool_result":
                    turns.append(Turn(kind=KIND_TOOL_RESULT, role=rtype,
                                      tool_use_id=str(block.get("tool_use_id", "")),
                                      text=_text_of_content(block.get("content")),
                                      extra={"is_error": bool(block.get("is_error", False))},
                                      **base))
                # thinking blocks are intentionally ignored
            if text_parts:
                kind = KIND_USER_TEXT if rtype == "user" else KIND_ASSISTANT_TEXT
                turns.insert(0, Turn(kind=kind, role=rtype, text="\n".join(text_parts), **base))
            return turns
    return [Turn(kind=KIND_OTHER, role=rtype if rtype in ("user", "assistant", "system") else "", **base)]


def iter_turns(path: str | Path, after_uuid: str | None = None,
               include_sidechain: bool = False, stats: ReadStats | None = None) -> Iterator[Turn]:
    """Yield Turns from a transcript, optionally only those after ``after_uuid``.

    ``after_uuid`` is a watermark: rows up to and including that uuid are
    skipped. If the uuid is never seen (e.g. the file was rotated), all rows
    are yielded so nothing is silently lost.
    """
    stats = stats if stats is not None else ReadStats()
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    buffered: list[Turn] = []
    found_watermark = after_uuid is None
    with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stats.lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                stats.errors += 1
                continue
            if not isinstance(row, dict):
                stats.errors += 1
                continue
            stats.parsed += 1
            if row.get("version"):
                stats.versions.add(str(row["version"]))
            turns = rows_to_turns(row)
            for t in turns:
                if t.is_sidechain and not include_sidechain:
                    stats.skipped_sidechain += 1
                    continue
                if t.kind == KIND_COMPACT_BOUNDARY:
                    stats.compactions += 1
                if t.uuid:
                    stats.last_uuid = t.uuid
                if not found_watermark:
                    if t.uuid == after_uuid:
                        found_watermark = True
                        buffered.clear()
                        continue
                    buffered.append(t)
                    continue
                yield t
    if not found_watermark:
        # Watermark never seen: replay everything we buffered.
        for t in buffered:
            yield t


def read_turns(path: str | Path, after_uuid: str | None = None, **kw) -> tuple[list[Turn], ReadStats]:
    stats = ReadStats()
    turns = list(iter_turns(path, after_uuid=after_uuid, stats=stats, **kw))
    return turns, stats


def capture_transcript(src: str | Path, dest_dir: str | Path, label: str) -> Path:
    """Copy a transcript verbatim (gzipped) into ``dest_dir``. Never raises on
    a missing source: returns the intended path with no file created."""
    src_p, dest = Path(src), Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{label}.jsonl.gz"
    if not src_p.exists():
        return out
    with open(src_p, "rb") as fin, gzip.open(out, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return out
