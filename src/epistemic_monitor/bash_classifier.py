"""Tier 0 classifier: does this Bash command write a source file? (DESIGN.md Appendix A)

Deterministic, sub-millisecond, conservative: ambiguity resolves toward
``writes_source`` or ``unknown``, never silently toward ``no_source_write``.
"""

from __future__ import annotations

import os
import re
import shlex
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
    # (?s): a heredoc body (`python3 - <<EOF`) sits on the lines after the interpreter (review 2026-11-27)
    ("py_write",       r'(?s)\bpython3?\b.*?(?:\bopen\([^)]*,\s*[\'"][wa]\+?[\'"]|\.write_(?:text|bytes)\(|'
                       r'\bos\.(?:remove|unlink|rename|replace)\(|\bshutil\.(?:move|copy\w*|rmtree)\()'),
    ("node_write",     r'(?s)\bnode\b.*?\b(?:writeFileSync|writeFile|appendFileSync|unlinkSync|rmSync|renameSync)\('),
    ("find_delete",    r'\bfind\b[^;&|\n]*\s-(?:delete|exec\s+rm)\b'),
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


# Rules whose regex names no path: the written files are the command's positional arguments (sed -i, black, git
# restore) or string literals (python3 -c "open('x','w')"). Without them a `path_glob_forbidden` falsifier never
# tripped on a one-step Bash write (review 2026-11-27).
_ARG_RULES = {"sed_inplace", "perl_inplace", "formatter", "patch_apply", "git_worktree", "codemod", "find_delete"}
_LITERAL_RULES = {"py_write", "node_write"}
_SEGMENT_END = re.compile(r'&&|\|\||[;|&\n]')
_Q = r'[rbf]?([\'"])([^\'"\n]+)\1'
_LITERAL_RXS = [  # (regex, groups holding a written path); a read-only open('x') is not a write
    (re.compile(r'\bopen\(\s*' + _Q + r'\s*,\s*(?:mode\s*=\s*)?[rbf]?[\'"][rbt]*[wax]'), (2,)),
    (re.compile(r'\bPath\(\s*' + _Q + r'\s*\)\s*\.\s*(?:write_text|write_bytes|unlink|rename|replace|touch|rmdir)\b'), (2,)),
    (re.compile(r'\bos\.(?:remove|unlink|truncate)\(\s*' + _Q), (2,)),
    (re.compile(r'\b(?:os\.(?:rename|replace)|shutil\.move)\(\s*' + _Q + r'\s*,\s*' + _Q), (2, 4)),
    (re.compile(r'\bshutil\.(?:copy\w*)\(\s*' + _Q + r'\s*,\s*' + _Q), (4,)),
    (re.compile(r'\bshutil\.rmtree\(\s*' + _Q), (2,)),
    (re.compile(r'\b(?:writeFileSync|writeFile|appendFileSync|unlinkSync|rmSync)\(\s*' + _Q), (2,)),
    (re.compile(r'\brenameSync\(\s*' + _Q + r'\s*,\s*' + _Q), (2, 4)),
]
_PATHLIKE = re.compile(r'^[~.$A-Za-z0-9_/][^\s*?]*(?:/|\.[A-Za-z0-9]+)$|^[A-Za-z0-9_.-]+/[^\s*?]*$')
_SUBCOMMANDS = {"format", "check", "apply", "am", "restore", "checkout", "revert", "cherry-pick", "merge", "rebase",
                "stash", "pop", "reset", "git", "ruff", "biome", "npx"}
_OPT_WITH_ARG = {"<", ">", "--source", "-s", "--config", "-c", "-name", "-iname", "-type", "-path", "-o", "-e", "-f",
                 "--expression", "--file", "-p"}


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _arg_paths(rule: str, command: str, start: int) -> list[str]:
    """Positional file arguments of the simple command a rule matched. Best effort: a path it misses is a falsifier
    that does not trip, which is today's behaviour; a non-path it keeps can only match a glob that names it."""
    end = _SEGMENT_END.search(command, start)
    toks = _tokens(command[start:end.start() if end else len(command)])[1:]  # drop the program
    if rule in ("sed_inplace", "perl_inplace"):
        script_given = False
        positional: list[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in ("-e", "-f", "--expression", "--file") or (rule == "perl_inplace" and t.startswith("-")
                                                                 and not t.startswith("--") and "e" in t[1:]):
                script_given = True
                i += 2
                continue
            if t in ("<", ">"):
                i += 2
                continue
            if not (t.startswith("-") and len(t) > 1):
                positional.append(t)
            i += 1
        files = positional if script_given else positional[1:]  # sed's first positional is its script
        return [p for p in files if not _is_non_source(p)]
    if rule == "git_worktree" and "--" in toks:
        return [p for p in toks[toks.index("--") + 1:] if not _is_non_source(p)]
    out: list[str] = []
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if t in _OPT_WITH_ARG:
            skip = True
            continue
        if t.startswith("-") or t in _SUBCOMMANDS:
            continue
        if rule == "patch_apply" and t.endswith((".diff", ".patch")):
            continue  # the patch itself is read; the files it touches are inside it
        if _PATHLIKE.match(t) and not _is_non_source(t):
            out.append(t)
    return out


def _literal_paths(command: str) -> list[str]:
    out: list[str] = []
    for rx, groups in _LITERAL_RXS:
        for m in rx.finditer(command):
            out.extend(m.group(g) for g in groups if not _is_non_source(m.group(g)))
    return out


# `bash x.sh`, `sh -e x.sh`, `source x.sh`, `. x.sh`, `./x.sh`: a script whose writes are not in the command text
# (2611.19764: individually harmless steps chained through a workspace file).
_SCRIPT_RUN = re.compile(r'(?:^|[;&|(]\s*|\s)(?:(?:ba|z|da)?sh\s+(?:-[a-zA-Z]+\s+)*|source\s+|\.\s+)?'
                         r'(?P<s>(?:\./|/|~/)?[^\s;|&<>()\'"`$]+\.(?:sh|bash))(?![\w.])')
_SCRIPT_MAX_BYTES = 64 * 1024


def script_write_paths(command: str, cwd: str) -> list[str]:
    """Write targets of the local shell scripts a command runs, one level deep. Reads only a regular file of at most
    64 KB with no NUL byte in its first 4 KB; comment lines are dropped, so a commented-out `rm` does not count."""
    out: list[str] = []
    for m in _SCRIPT_RUN.finditer(command):
        name = os.path.expanduser(m.group("s"))
        path = name if os.path.isabs(name) else os.path.join(cwd or ".", name)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > _SCRIPT_MAX_BYTES:
                continue
            with open(path, "rb") as fh:
                raw = fh.read(_SCRIPT_MAX_BYTES + 1)
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        text = "\n".join(ln for ln in raw.decode("utf-8", "replace").splitlines() if not ln.lstrip().startswith("#"))
        for p in classify(text).paths:
            if p not in out:
                out.append(p)
    return out


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
            elif name in _ARG_RULES:
                paths.extend(_arg_paths(name, command, m.start()))
            elif name in _LITERAL_RULES:
                paths.extend(_literal_paths(command[m.start():]))
    if hits:
        return Classification(WRITES_SOURCE, sorted(set(hits)), sorted(set(paths)))
    if _OPAQUE.search(command) and not _READONLY.match(command):
        return Classification(UNKNOWN, ["opaque"], [])
    return Classification(NO_SOURCE_WRITE, [], [])
