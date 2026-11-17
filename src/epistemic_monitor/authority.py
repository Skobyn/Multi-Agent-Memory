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

_ID_RE = re.compile(r"\b([DCPKR])-0*(\d{1,6})\b", re.IGNORECASE)
_CLI_RE = re.compile(r"em-hook|epistemic_monitor|(?:^|[\s;&|(`'\"/])em\s+ledger\b")
_LEDGER_RE = re.compile(r"\bledger\b")
# confirm; --user and its argparse abbreviations (--u, --us, --use); --by user / --by=user and abbreviations (--b)
_MARKER_RE = re.compile(r"\bconfirm\b|--u(?:s(?:er?)?)?(?![\w-])|--by?(?:=|\s+)['\"]?user\b", re.IGNORECASE)
_HOOK_UPS_RE = re.compile(r"\bhook\b.*user[_-]prompt[_-]submit", re.IGNORECASE | re.DOTALL)
_LEDGER_ARGS_RE = re.compile(r"<command-name>/?(?:[\w.-]+:)?ledger</command-name>\s*<command-args>(.*?)</command-args>",
                             re.DOTALL)
# one ledger invocation's arguments: from `ledger` to the next shell separator or redirection
_INVOCATION_RE = re.compile(r"\bledger['\"]?\s+([^;&|\n<>`]*)")
_REDIRECT_RE = re.compile(r"\s\d*[<>]+&?\d*")


def _canon_id(m: re.Match) -> str:
    return f"{m.group(1).upper()}-{int(m.group(2)):04d}"


def normalize(args: str) -> tuple[str, ...]:
    """Ledger arguments as comparable tokens: shell-split, quotes dropped, ids canonical, ``--by=user`` split."""
    try:
        toks = shlex.split(args)
    except ValueError:
        toks = args.split()
    out: list[str] = []
    for t in toks:
        t = t.strip("'\"")
        if t.startswith("--") and "=" in t:
            k, v = t.split("=", 1)
            out.extend([k, v])
        elif t:
            out.append(t)
    return tuple(_ID_RE.sub(_canon_id, t) for t in out)


def needs_check(command: str) -> bool:
    """Cheap pre-test: does this Bash command touch the plugin CLI at all?"""
    return bool(command) and bool(_CLI_RE.search(command))


def invocations(command: str) -> list[tuple[str, ...]]:
    """Normalised argument lists of every ``ledger ...`` invocation in a command or a user's text."""
    text = _REDIRECT_RE.sub(" ; ", command or "")      # `2>&1`, `> out.txt` end an invocation
    return [normalize(m.group(1)) for m in _INVOCATION_RE.finditer(text)]


def granted_invocations(user_text: str) -> list[tuple[str, ...]]:
    """Ledger commands the user's own turn spells out: a ``/ledger`` slash command, or a pasted CLI line."""
    out = [inv for m in _LEDGER_ARGS_RE.finditer(user_text or "") for inv in invocations("ledger " + m.group(1))]
    if _CLI_RE.search(user_text or ""):
        out.extend(invocations(user_text))
    return out


def check(command: str, last_user_text: str | None, used: set[tuple[str, ...]] | None = None) -> tuple[str | None, list[tuple[str, ...]]]:
    """(reason to refuse or None, the granted invocations this command uses).

    ``used`` holds invocations already run under the same user turn: a grant is good for one run."""
    if not needs_check(command):
        return None, []
    if _HOOK_UPS_RE.search(command):
        return ("Epistemic monitor: running the user_prompt_submit hook by hand would apply SUPERSEDE lines with the "
                "user's authority. Claude Code runs hooks itself; do not run it from Bash."), []
    if not (_LEDGER_RE.search(command) and _MARKER_RE.search(command)):
        return None, []
    used = used or set()
    granted = granted_invocations(last_user_text or "")
    wanted = [inv for inv in invocations(command) if _MARKER_RE.search(" ".join(inv))]
    ok = bool(wanted) and all(inv in granted and inv not in used for inv in wanted)
    if ok:
        return None, wanted
    shown = " ".join(wanted[0]) if wanted else "<subcommand> <id> ..."
    return ("Epistemic monitor: this ledger command acts with the user's authority (`confirm`, `--user` or "
            "`--by user`), and the user's latest message does not contain this exact command (or it already ran). "
            "Only the user can grant that. Do not retry or rephrase it; ask them, and if they want it they can type "
            f"`/ledger {shown}` themselves (or `SUPERSEDE <id>: <reason>` to change a decision)."), []
