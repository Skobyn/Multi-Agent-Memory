"""Mechanical anchor extraction (DESIGN.md §3, §7), after Compact-Bench's
seven literal kinds plus salient terms. Anchors make the compaction audit a
deterministic substring test."""

from __future__ import annotations

import re

_PATH = re.compile(r"(?<![\w/])(?:[\w.@-]+/)+[\w.@-]+(?:\.[A-Za-z0-9]+)?")
_SHA = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_ISSUE = re.compile(r"(?<![\w/])#\d{1,7}\b")
_ERROR = re.compile(r"\b[A-Z][A-Za-z]+(?:Error|Exception|Warning)\b")
_URL = re.compile(r"https?://[^\s)\]>\"']+")
_KV = re.compile(r"\b[A-Z][A-Z0-9_]{2,}=[^\s,;]+")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_TERM = re.compile(r"\b(?:[A-Z][a-z]+[A-Z][A-Za-z0-9]*|[A-Z][A-Z0-9]{2,}|[A-Za-z][a-z]*[A-Z][A-Za-z0-9]*|[a-z]+_[a-z_0-9]+|[A-Z][a-z]{3,})\b")

_STOP = {
    "The", "This", "That", "These", "Those", "When", "Where", "What", "Which", "While", "With", "Without", "Then",
    "There", "Here", "They", "Their", "From", "Into", "Only", "Also", "Never", "Always", "Keep", "Make", "Must",
    "Should", "Would", "Could", "Before", "After", "Every", "Each", "Some", "Such", "Than", "Because", "Please",
    "Yes", "Not", "Use", "Using", "Decision", "Constraint", "User", "Agent", "Session", "Turn", "Note", "Prefer",
    "Avoid", "Ensure", "Don", "Does", "Have", "Will", "Need", "Want", "Project", "Code", "File", "Files", "Tests",
    "Test", "Run", "Add", "Remove", "Change", "Changes", "Primary", "Default", "Instead", "Still", "Just", "Until",
}

MAX_PER_KIND = 40


def extract_anchors(*texts: str, max_terms: int = 8) -> list[str]:
    text = "\n".join(t for t in texts if t)
    out: list[str] = []
    seen: set[str] = set()

    def add(values):
        n = 0
        for v in values:
            v = v.strip().rstrip(".,;:")
            if not v or v in seen or n >= MAX_PER_KIND:
                continue
            seen.add(v)
            out.append(v)
            n += 1

    add(_URL.findall(text)[:MAX_PER_KIND])
    add(p for p in _PATH.findall(text) if not p.startswith("http") and "/" in p and len(p) <= 120)
    add(_SHA.findall(text))
    add(_ISSUE.findall(text))
    add(_ERROR.findall(text))
    add(_KV.findall(text))
    add(_IP.findall(text))
    terms = [t for t in _TERM.findall(text) if t not in _STOP and len(t) >= 4 and not t.islower()]
    # prefer technology-looking tokens (mixed case / digits / underscores) over plain capitalized words
    terms.sort(key=lambda t: (not (any(c.isdigit() for c in t) or "_" in t or (t[1:] != t[1:].lower() and t[1:] != t[1:].upper())), len(t)))
    add(terms[:max_terms])
    return out
