"""Origin check for ledger commands that carry user authority (Tier 0, deterministic).

The agent has Bash, and ``/ledger`` runs the CLI through the agent, so ``ledger confirm``, ``add --user`` and
``--by user`` were only as trustworthy as the agent (docs/RESEARCH-2026-11-13.md, specified item 1). EAL-Bench
(arXiv 2609.01836) measures exactly this: an agent's own memory writes grant authority the source history never
gave, and executors act on it. Before such a Bash call runs, this module asks whether the user's latest turn
(read from the transcript, role-typed, harness envelopes excluded) contains the same ledger command. A typed
``/ledger confirm D-0007`` is stored in the transcript as ``<command-name>/ledger</command-name>
<command-args>confirm D-0007</command-args>`` (verified against Claude Code 2.1.325), so the slash command passes.

Detection is deliberately broad (any CLI mention plus any authority marker anywhere in the string, including
argparse prefix abbreviations such as ``--us`` and ``--b user``, inside ``bash -c`` or after ``&``); the grant is
deliberately exact (the agent's ledger arguments must equal the user's after normalising quotes and ids).

``em-hook hook user_prompt_submit`` run from Bash is always refused: that handler applies ``SUPERSEDE`` lines with
user authority to whatever payload arrives on stdin, so a forged payload was a second path to the same power.

This stops a confused or steered agent on the normal CLI path. It is not sound against an agent determined to
bypass it (shell variables, ``xargs``, ``python3 -c`` with the store, or writing ledger.jsonl): nothing in-process is.
"""
from __future__ import annotations

import re
import shlex

from .transcript import split_third_party

_ID_RE = re.compile(r"\b([DCPKR])-0*(\d{1,6})\b", re.IGNORECASE)
_CLI_RE = re.compile(r"em-hook|epistemic_monitor|(?:^|[\s;&|(`'\"/])em\s+ledger\b")
_LEDGER_RE = re.compile(r"\bledger\b")
# confirm; --user and its argparse abbreviations (--u, --us, --use); --by user / --by=user and abbreviations (--b)
_MARKER_RE = re.compile(r"\bconfirm\b|--u(?:s(?:er?)?)?(?![\w-])|--by?(?:=|\s+)['\"]?user\b", re.IGNORECASE)
# Subcommands that only the user may run, matched by name rather than a marker (review 2026-11-27, after PROVE,
# arXiv 2611.34215: a verifier fed outcome feedback drifts into accepting what it should refuse). `label` and
# `calibrate` set the decision model's per-type thresholds for every later session, so an agent that labels its own
# denied call "ok" and recalibrates would loosen the gate on the user's rules. `mark-good` feeds a quantile the gate
# does not read yet; it joins this set when it does.
_USER_ONLY_CMDS = frozenset({"label", "calibrate"})
_HOOK_UPS_RE = re.compile(r"\bhook\b.*user[_-]prompt[_-]submit", re.IGNORECASE | re.DOTALL)
_LEDGER_ARGS_RE = re.compile(r"<command-name>/?(?:[\w.-]+:)?ledger</command-name>\s*<command-args>(.*?)</command-args>",
                             re.DOTALL)
# a redirection token: `2>&1`, `>`, `>>`, `<`, `>out.txt`
_REDIRECT_TOK_RE = re.compile(r"^\d*(?:>>?|<)(&\d*|.*)$")
_SEPARATORS = ";&|\n()`"


def _canon_id(m: re.Match) -> str:
    return f"{m.group(1).upper()}-{int(m.group(2)):04d}"


def segments(text: str) -> list[str] | None:
    """Simple commands in a shell string, split at ``; & | newline ( ) ` `` outside quotes (an ``&`` right after ``<``
    or ``>`` is part of a redirection). None when the quotes do not balance: nothing can be compared exactly then.
    Separators inside quotes are argument text (review of #25: ``--statement "a; b"`` must not end the invocation)."""
    segs: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(text or "")
    while i < n:
        c = text[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(text[i + 1]); i += 2; continue
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c; cur.append(c)
        elif c == "\\" and i + 1 < n:
            cur.append(c); cur.append(text[i + 1]); i += 2; continue
        elif c in _SEPARATORS and not (c == "&" and cur and cur[-1] in "<>"):
            segs.append("".join(cur)); cur = []
        else:
            cur.append(c)
        i += 1
    if quote:
        return None
    segs.append("".join(cur))
    return [x for x in segs if x.strip()]


def _tokens(segment: str) -> list[str]:
    """shlex tokens of one simple command, redirections dropped."""
    out: list[str] = []
    toks = shlex.split(segment)
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if _REDIRECT_TOK_RE.match(t):
            skip = t.rstrip("0123456789") in (">", ">>", "<")  # a bare operator: its target is the next token
            continue
        out.append(t)
    return out


def normalize(args: str | list[str]) -> tuple[str, ...]:
    """Ledger arguments as comparable tokens: shell-split, quotes dropped, ids canonical, ``--by=user`` split."""
    if isinstance(args, str):
        try:
            toks = shlex.split(args)
        except ValueError:
            toks = args.split()
    else:
        toks = list(args)
    out: list[str] = []
    for t in toks:
        t = t.strip("'\"")
        if t.startswith("--") and "=" in t:
            k, v = t.split("=", 1)
            out.extend([k, v])
        elif t:
            out.append(t)
    return tuple(_ID_RE.sub(_canon_id, t) for t in out)


def expands(text: str) -> bool:
    """Does the shell expand anything here: ``$`` or a backtick outside single quotes, or a glob, brace or tilde
    character outside any quotes (none of them backslash-escaped)?
    The comparison is on text after quotes are removed, so ``"Budget is $LIMIT"`` matched a user's literal
    ``'Budget is $LIMIT'`` while the shell stored whatever ``LIMIT`` held (review, 2026-11-18)."""
    quote: str | None = None
    i, n = 0, len(text or "")
    while i < n:
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "\\" and i + 1 < n:
            i += 2
            continue
        elif c in "$`":
            return True
        elif quote is None and c in "*?[{~":   # glob, brace and tilde expansion (review, 2026-11-18)
            return True
        elif c == '"':
            quote = None if quote == '"' else '"'
        elif c == "'" and quote is None:
            quote = "'"
        i += 1
    return False


def needs_check(command: str) -> bool:
    """Cheap pre-test: does this Bash command touch the plugin CLI at all?"""
    return bool(command) and bool(_CLI_RE.search(command))


def invocations(command: str) -> list[tuple[str, ...]]:
    """Normalised argument lists of every ``ledger ...`` invocation in a command, compared whole: quote-aware, so
    shell punctuation inside a quoted argument stays in that argument. Unbalanced quotes yield nothing."""
    out: list[tuple[str, ...]] = []
    for seg in segments(command or "") or []:
        try:
            toks = _tokens(seg)
        except ValueError:
            continue
        for k, t in enumerate(toks):
            if t == "ledger" or t.endswith("/ledger"):
                out.append(normalize(toks[k + 1:]))
                break
    return out


def only_ledger(command: str) -> bool:
    """Is this Bash call nothing but ledger CLI invocations? Only then may a granted command skip the gate (review
    of #25: ``em-hook ledger confirm D-0007 && rm x`` must still be gated for the ``rm``)."""
    if not command or "$(" in command or "`" in command:
        return False
    segs = segments(command)
    if not segs:
        return False
    for seg in segs:
        try:
            toks = _tokens(seg)
        except ValueError:
            return False
        if not (_CLI_RE.search(seg) and any(t == "ledger" or t.endswith("/ledger") for t in toks)):
            return False
    return True


def _subcommand(inv: tuple[str, ...]) -> str:
    return next((t for t in inv if not t.startswith("-")), "")


def _user_only(inv: tuple[str, ...]) -> bool:
    return bool(_MARKER_RE.search(" ".join(inv))) or _subcommand(inv) in _USER_ONLY_CMDS


def granted_invocations(user_text: str) -> list[tuple[str, ...]]:
    """Ledger commands the user's own turn spells out: a ``/ledger`` slash command, or a pasted CLI line (one line;
    a line whose quotes do not balance grants nothing)."""
    user_text = split_third_party(user_text or "")[0]   # an external event's text inside the turn grants nothing
    out = [inv for m in _LEDGER_ARGS_RE.finditer(user_text or "") for inv in invocations("ledger " + m.group(1))]
    for line in (user_text or "").splitlines():
        if _CLI_RE.search(line):
            out.extend(invocations(line))
    return out


def check(command: str, last_user_text: str | None, used: set[tuple[str, ...]] | None = None) -> tuple[str | None, list[tuple[str, ...]]]:
    """(reason to refuse or None, the granted invocations this command uses).

    ``used`` holds invocations already run under the same user turn: a grant is good for one run."""
    if not needs_check(command):
        return None, []
    if _HOOK_UPS_RE.search(command):
        return ("Epistemic monitor: running the user_prompt_submit hook by hand would apply SUPERSEDE lines with the "
                "user's authority. Claude Code runs hooks itself; do not run it from Bash."), []
    if not _LEDGER_RE.search(command):
        return None, []
    wanted = [inv for inv in invocations(command) if _user_only(inv)]
    if not (_MARKER_RE.search(command) or wanted):
        return None, []
    used = used or set()
    granted = granted_invocations(last_user_text or "")
    ok = bool(wanted) and all(inv in granted and inv not in used for inv in wanted)
    if ok and expands(command):
        return ("Epistemic monitor: this ledger command acts with the user's authority, and the shell would expand "
                "`$` or a backtick in it, so what is stored could differ from what the user typed. Run it with the "
                "arguments in single quotes, exactly as the user wrote them."), []
    if ok:
        return None, wanted
    shown = " ".join(wanted[0]) if wanted else "<subcommand> <id> ..."
    return ("Epistemic monitor: this ledger command acts with the user's authority (`confirm`, `--user`, "
            "`--by user`, or `label`/`calibrate`, which set the gate's thresholds), and the user's latest message does not contain this exact command (or it already ran). "
            "Only the user can grant that. Do not retry or rephrase it; ask them, and if they want it they can type "
            f"`/ledger {shown}` themselves (or `SUPERSEDE <id>: <reason>` to change a decision)."), []
