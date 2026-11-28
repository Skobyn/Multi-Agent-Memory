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


# --- does a command RUN something matching a pattern? (process falsifiers, RESEARCH-2026-11-28) -----------------------
# `command_requires_prior` matched its regexes against the raw command text, so `echo pytest`, `grep -r pytest .` or
# `git log --grep pytest` counted as the tests having run and cleared the next commit without a model call (SkillCFI,
# arXiv 2611.24185: an obligation is enforceable only on evidence that crosses the tool boundary). The same text match
# made `grep -r "git commit" .` demand a prior test run. A segment whose program only prints, searches or reads text
# does not run what it names. Anything else (bash -c, xargs, docker run, make) still counts: this errs toward
# matching, which keeps a doubtful prior step satisfied and a doubtful trigger live, as before.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|&\n`]|\$\(|\(|\)")
_INERT_PROGRAMS = frozenset({
    "echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "cat", "less", "more", "head", "tail", "man",
    "which", "whereis", "type", "history", "ls", "wc", "sort", "uniq", ":", "true", "false", "#",
})
_INERT_GIT = frozenset({"log", "show", "grep", "blame", "diff", "status", "help", "reflog", "shortlog"})
_PREFIX_WORDS = frozenset({"sudo", "time", "nice", "nohup", "exec", "command", "builtin", "env", "then", "do",
                           "else", "!", "{"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# flags that make a runner list, explain or describe instead of run. The short forms mean something else to many
# tools (`pg_dump -h host`, `ctest -V`, `docker run -h name`), so they count only for the test runners named here.
_DRY_FLAGS = frozenset({"--co", "--collect-only", "--help", "--version", "--dry-run"})
_SHORT_DRY_FLAGS = frozenset({"-h", "-V"})
_SHORT_DRY_PROGRAMS = frozenset({"pytest", "py.test", "tox", "nox"})
_WORD = re.compile(r"\S+")
_MAX_COMMAND = 65536


def _program(segment: str) -> tuple[str, list[str], int]:
    """(program name, its arguments, end offset in ``segment`` of the program word, or of the git subcommand)."""
    words = list(_WORD.finditer(segment))
    i = 0
    while i < len(words) and (words[i].group() in _PREFIX_WORDS or _ASSIGNMENT.match(words[i].group())):
        i += 1
    if i >= len(words):
        return "", [], 0
    prog, end = words[i].group().rsplit("/", 1)[-1], words[i].end()
    args = [w.group() for w in words[i + 1:]]
    if prog == "git":
        sub = next((w for w in words[i + 1:] if not w.group().startswith("-")), None)
        if sub is not None:
            end = sub.end()
    return prog, args, end


def segment_is_inert(segment: str, rx: re.Pattern | None = None) -> bool:
    """True when the segment's program only prints, searches or reads text, or a runner is asked for a dry mode. A
    pattern that matches the program word itself (a rule whose step IS `git status` or `ls dist`) is not made
    inert by it: only a mention in the arguments is."""
    prog, args, end = _program(segment)
    if not prog or prog.startswith("#"):
        return True
    if any(a in _DRY_FLAGS for a in args) or (prog in _SHORT_DRY_PROGRAMS and any(a in _SHORT_DRY_FLAGS for a in args)):
        return True
    if rx is not None and any(m.start() < end for m in rx.finditer(segment)):
        return False
    if prog in _INERT_PROGRAMS:
        return True
    return prog == "git" and next((a for a in args if not a.startswith("-")), "") in _INERT_GIT


def runs_matching(command: str, pattern: str) -> bool:
    """Does ``command`` run something that ``pattern`` matches? The pattern is tried against each shell segment
    (split on ; && || | & newlines, subshells and substitutions) whose program is not inert. A pattern that matches
    no single segment but does match the whole command (it spans an operator) falls back to the whole-text match.
    Raises ``re.error`` on a bad pattern, as ``re.search`` does."""
    rx = re.compile(pattern)
    text = re.sub(r"(?m)(^|\s)#.*$", r"\1", (command or "")[:_MAX_COMMAND])  # an unquoted comment runs nothing
    any_segment = False
    for seg in _SEGMENT_SPLIT.split(text):
        if not seg.strip() or not rx.search(seg):
            continue
        any_segment = True
        if not segment_is_inert(seg, rx):
            return True
    return not any_segment and bool(rx.search(text))
