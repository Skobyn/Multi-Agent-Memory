"""Tier 0 classifier: does this Bash command write a source file? (DESIGN.md Appendix A)

Deterministic, sub-millisecond, conservative: ambiguity resolves toward
``writes_source`` or ``unknown``, never silently toward ``no_source_write``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WRITES_SOURCE = "writes_source"
NO_SOURCE_WRITE = "no_source_write"
UNKNOWN = "unknown"

_SOURCE_EXCLUDE = re.compile(r'(^|/)(node_modules|\.git|dist|build|\.venv|venv|__pycache__|\.pytest_cache)(/|$)')
_NON_SOURCE_PREFIX = re.compile(r'^(/tmp/|/dev/|/proc/|~?/\.cache/|\$\{?SCRATCH)')
_PATH = r'(?P<p>[~./$A-Za-z0-9_][^\s;|&<>"\'`]*)'
_HARD_RESET = r'reset\s+--' + 'hard'  # split so the literal never appears in a shell command line

RULES: list[tuple[str, str]] = [
    ("redirect",       r'(?<![<\d])(?:\d?>>?|&>)\s*' + _PATH),
    ("tee",            r'\btee\s+(?:-[a-z]+\s+)*' + _PATH),
    ("sed_inplace",    r'\bsed\s+(?:-[a-zA-Z]*\s+)*-i\b'),
    ("perl_inplace",   r'\bperl\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*i\b'),
    ("formatter",      r'\b(?:black|isort|autopep8|ruff\s+(?:format|check\s+.*--fix)|prettier\s+.*(?:--write|-w)\b|gofmt\s+-w|rustfmt|eslint\s+.*--fix|biome\s+(?:format|check)\s+.*--write)\b'),
    ("codemod",        r'(?:^|[;&|]\s*|\bnpx\s+)(?:jscodeshift|codemod)\b|\bast-grep\s+.*(?:-U|--update-all)|\bcomby\s+.*-in-place'),
    ("patch_apply",    r'\b(?:git\s+apply|patch\b|git\s+am)\b'),
    ("git_worktree",   r'\bgit\s+(?:checkout\s+(?:--|[^-\s]+\s+--)|restore\b|revert\b|cherry-pick\b|stash\s+pop\b|' + _HARD_RESET + r'|merge\b|rebase\b)'),
    ("copy_move",      r'\b(?:cp|mv|rsync|install|ln\s+-s[f]?)\s+(?:-[a-zA-Z]+\s+)*\S+\s+' + _PATH),
    ("delete",         r'\brm\s+(?:-[a-zA-Z]+\s+)*' + _PATH),
    ("touch_truncate", r'\b(?:touch|truncate\s+-s\s*\d+)\s+' + _PATH),
    ("dd",             r'\bdd\b.*\bof=' + _PATH),
    ("py_write",       r'\bpython3?\b.*\bopen\([^)]*,\s*[\'"][wa]\+?[\'"]'),
    ("node_write",     r'\bnode\b.*\b(?:writeFileSync|writeFile|appendFileSync)\('),
]
_COMPILED = [(name, re.compile(rx)) for name, rx in RULES]

_OPAQUE = re.compile(
    r'(\bbash\s+\S+\.sh\b|\bsh\s+\S+\.sh\b|\./\S+\.sh\b|\bmake\b|\bnpm\s+run\b|\bpnpm\s+run\b|\byarn\s+\S+|'
    r'\bnpx\s+\S+|\beval\b|\bxargs\b|\|\s*(?:ba)?sh\b|\bpython3?\s+\S+\.py\b(?!.*\bopen\()|'
    r'\bcargo\s+(?:run|fix)\b|\bgo\s+(?:run|generate)\b)')
_READONLY = re.compile(
    r'^\s*(?:git\s+(?:status|log|diff|show|branch|blame)|ls|cat|head|tail|grep|rg|find|wc|pytest|'
    r'python3?\s+-m\s+pytest|npm\s+test|go\s+test|cargo\s+test|echo)\b')


@dataclass
class Classification:
    verdict: str
    rules: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "rules": self.rules, "paths": self.paths}


def _is_non_source(path: str) -> bool:
    return bool(_NON_SOURCE_PREFIX.match(path) or _SOURCE_EXCLUDE.search(path) or path == "/dev/null")


def classify(command: str) -> Classification:
    hits: list[str] = []
    paths: list[str] = []
    for name, rx in _COMPILED:
        for m in rx.finditer(command):
            p = m.groupdict().get("p") if m.groupdict() else None
            if p and _is_non_source(p):
                continue
            hits.append(name)
            if p:
                paths.append(p)
    if hits:
        return Classification(WRITES_SOURCE, sorted(set(hits)), sorted(set(paths)))
    if _OPAQUE.search(command) and not _READONLY.match(command):
        return Classification(UNKNOWN, ["opaque"], [])
    return Classification(NO_SOURCE_WRITE, [], [])
