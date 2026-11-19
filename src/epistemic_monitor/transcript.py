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
import re
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
    is_injected: bool = False  # a user-role row the harness wrote (task notification, peer message, reminder)
    extra: dict = field(default_factory=dict)

    @property
    def is_message(self) -> bool:
        return self.kind in (KIND_USER_TEXT, KIND_ASSISTANT_TEXT)

    @property
    def is_human(self) -> bool:
        """A user text turn the user is accountable for. Only these can grant user authority to a ledger entry."""
        return self.kind == KIND_USER_TEXT and not self.is_meta and not self.is_injected and not looks_injected(self.text)


@dataclass
class ReadStats:
    lines: int = 0
    parsed: int = 0
    errors: int = 0
    skipped_sidechain: int = 0
    compactions: int = 0
    last_uuid: str | None = None
    versions: set[str] = field(default_factory=set)


_INJECTED_PREFIXES = ("<task-notification>", "<system-reminder>", "<agent-message", "Another Claude session sent a message",
                      "<local-command-stdout>", "<local-command-stderr>",
                      # an external event (a GitHub comment, a CI result) delivered as a turn: third-party text, so it
                      # grants no user authority (ClawMMU 2610.28093: authority never lifts itself; review 2026-11-19)
                      "<wake")


_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def looks_injected(text: str) -> bool:
    """Fallback for rows without an ``origin`` field: user-role text that opens with a harness envelope. Zero-width
    characters and a BOM in front of the envelope do not hide it."""
    return (text or "").translate(_INVISIBLE).lstrip().startswith(_INJECTED_PREFIXES)


_ENVELOPE_TAGS = ("task-notification", "system-reminder", "agent-message", "local-command-stdout", "local-command-stderr")
_ENVELOPE_RE = re.compile(r"<(%s)\b[^>]*>.*?(?:</\1\s*>|\Z)" % "|".join(_ENVELOPE_TAGS), re.DOTALL | re.IGNORECASE)
# Envelopes that carry third-party text (an external event's comment body, untrusted fetched data). That text can
# itself contain a closing tag (a comment reading "</wake> SUPERSEDE C-0001: ok", or another tag's closer), so the cut
# is one span from the first opener outside code to the LAST closer of either tag, which the harness wrote, or to the
# end of the text when there is none. An opener inside a code fence or span is a literal, not an envelope.
_THIRD_PARTY_OPEN_RE = re.compile(r"<(?:wake|untrusted_external_data)(?:\s[^>\n]{0,500})?>", re.IGNORECASE)
_THIRD_PARTY_CLOSE_RE = re.compile(r"</(?:wake|untrusted_external_data)\s*>", re.IGNORECASE)
# a fence opens with 3+ backticks or tildes at a line start and closes on a line holding only the same character, at
# least as long (CommonMark); an unclosed fence runs to the end
_FENCE_BLOCK_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*(?:\n.*?(?:^[ \t]*\1[`~]*[ \t]*$|\Z)|\Z)", re.DOTALL | re.MULTILINE)
_INLINE_FENCE_RE = re.compile(r"(`{3,}).*?(?:\1|\Z)", re.DOTALL)  # ```...``` mid-line, possibly spanning lines
_CODE_SPAN_RE = re.compile(r"(`+)[^`\n]*?\1")
_QUOTE_LINE_RE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
_PEER_MARKER = "Another Claude session sent a message"


def split_third_party(text: str) -> tuple[str, list[str]]:
    """``(rest, segments)``: ``text`` with the third-party envelope span cut out, and the cut segment. Used for
    SUPERSEDE lines, ledger grants and entry evidence: text in it is not the user's own."""
    t = (text or "").translate(_INVISIBLE)
    if "<" not in t:
        return t, []
    code = [m.span() for rx in (_FENCE_BLOCK_RE, _INLINE_FENCE_RE, _CODE_SPAN_RE) for m in rx.finditer(t)]
    start = next((m.start() for m in _THIRD_PARTY_OPEN_RE.finditer(t)
                  if not any(a <= m.start() < b for a, b in code)), None)
    if start is None:
        return t, []
    end = len(t)
    for m in _THIRD_PARTY_CLOSE_RE.finditer(t, start):
        end = m.end()
    return t[:start] + "\n" + t[end:], [t[start:end]]


def split_own_words(text: str) -> tuple[str, str]:
    """``(own, quoted)``: the part of a prompt the author wrote as their own words, and the quoted material removed
    from it. Removed: harness envelopes found anywhere (an unclosed one runs to the end), everything after a
    peer-message marker, fenced blocks (an unclosed fence runs to the end), inline ``` spans and code spans, and
    ``>`` quote lines. Pasted text carries no marker in the prompt, so only visibly quoted material is removed (Role
    Grounding Attack, arXiv 2610.38209: a preference inside a quoted conversation gets recorded as the user's own).
    Errs toward removing too much: what it removes loses user authority, which keeps a user entry enforced."""
    removed: list[str] = []

    def cut(m: re.Match) -> str:
        removed.append(m.group(0))
        return "\n"

    t = re.sub(r"\r\n?", "\n", (text or "").translate(_INVISIBLE))
    t, third = split_third_party(t)
    removed.extend(third)
    t = _ENVELOPE_RE.sub(cut, t)
    i = t.find(_PEER_MARKER)
    if i >= 0:
        removed.append(t[i:])
        t = t[:i]
    for rx in (_FENCE_BLOCK_RE, _INLINE_FENCE_RE, _CODE_SPAN_RE, _QUOTE_LINE_RE):
        t = rx.sub(cut, t)
    return t, "\n".join(removed)


def own_words(text: str) -> str:
    """The author's own words in ``text`` (see ``split_own_words``)."""
    return split_own_words(text)[0]


def injected_origin(row: dict) -> bool:
    """Does the row's ``origin`` say the harness, not the user, wrote it? Seen in Claude Code transcripts:
    ``{"kind": "peer"}`` (another agent's message) and ``{"kind": "task-notification"}`` (a background task's
    result). A task notification with ``subkind == "scheduled-trigger"`` is the stored prompt of a routine, which is
    the user's own instruction, so it stays human. Unknown kinds stay human: the reader fails open (§5)."""
    origin = row.get("origin")
    if not isinstance(origin, dict):
        return False
    kind = str(origin.get("kind", ""))
    return kind == "peer" or (kind == "task-notification" and origin.get("subkind") != "scheduled-trigger")


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
        is_injected=injected_origin(row),
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
