"""User-authority grants for the ledger CLI (docs/RESEARCH-2026-11-17.md, I1).

The agent has Bash, and `/ledger` runs the CLI through the agent, so `ledger confirm`, `add --user` and
`--by user` would otherwise grant user authority on the agent's say-so. A misled agent (MemoryDojo: injected
text that persists and steers; HANDBOOK.md: a plausible in-environment request overriding standing policy) runs
exactly that command. So when the CLI runs inside Claude Code it asks for a grant: the user's latest real prompt
in this session must name the same subcommand and entry id (`/ledger confirm D-0007`, or prose such as
"confirm D-0007"). Grants are recorded at UserPromptSubmit, expire, and are used once.

This is friction against a misled agent, not a boundary against a determined one: the agent can still edit the
data directory or unset the environment. Verified on Claude Code 2.1.x (2026-11-17): the UserPromptSubmit
payload carries the raw typed text (`/plugin:cmd args`, not the expanded body), and `CLAUDE_CODE_SESSION_ID` in
the Bash environment equals the hook payload's `session_id`.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

STATE_KEY = "ledger_grants"
GRANT_TTL_S = 30 * 60
_SUBS = ("confirm", "supersede", "resolve", "retire", "add", "dedupe")
_ID_RE = re.compile(r"\b([DCPKR]-\d{4,})\b", re.IGNORECASE)
# a typed slash command, plugin-namespaced or not: "/ledger confirm D-0007", "/epistemic-monitor:ledger add ..."
_SLASH_RE = re.compile(r"^\s*/(?:[\w.-]+:)?ledger\s+(" + "|".join(_SUBS) + r")\b([^\n]*)", re.IGNORECASE | re.MULTILINE)
# prose: "please confirm D-0007", "supersede C-0003 and C-0004"
_PROSE_RE = re.compile(r"\b(confirm|supersede|resolve|retire)\b([^\n.;]{0,60})", re.IGNORECASE)
# "retire D-0007" and "resolve D-0007" both mean closing it: `ledger retire` or `ledger resolve --retire`
_ALIASES = {"resolve": ("resolve", "retire"), "retire": ("retire", "resolve")}
_NEGATION_RE =re.compile(r"\b(not|don'?t|do not|never|no need to|shouldn'?t)\s*$", re.IGNORECASE)


def ledger_grants(prompt: str) -> list[list[Any]]:
    """[subcommand, entry id or None] pairs the user's own prompt authorizes."""
    grants: list[list[Any]] = []

    def add(sub: str, eid: str | None) -> None:
        g = [sub.lower(), eid.upper() if eid else None]
        if g not in grants:
            grants.append(g)

    text = prompt or ""
    for m in _SLASH_RE.finditer(text):
        sub, rest = m.group(1).lower(), m.group(2)
        ids = _ID_RE.findall(rest)
        if sub == "add":
            if "--user" in rest:
                add("add", None)
        elif sub == "dedupe":
            if "--by user" in rest or "--by=user" in rest:
                add("dedupe", None)
        else:
            for eid in ids:
                add(sub, eid)
    for m in _PROSE_RE.finditer(text):
        if _NEGATION_RE.search(text[max(0, m.start() - 20):m.start()]):
            continue
        for eid in _ID_RE.findall(m.group(2)):
            add(m.group(1), eid)
    return grants


def record_grants(store, session_id: str, prompt: str) -> None:
    """Called first thing in UserPromptSubmit, for a prompt the user is accountable for. Replaces older grants:
    only the latest real prompt counts."""
    store.set_state(session_id, STATE_KEY, {"ts": time.time(), "grants": ledger_grants(prompt)})


def inside_claude_code() -> bool:
    return os.environ.get("CLAUDECODE") == "1"


def check(store, sub: str, ids: list[str | None], consume: bool = True) -> tuple[bool, str]:
    """May this CLI call act with user authority? Outside Claude Code (the user's own terminal) always yes.
    Returns (ok, reason). On success the matched grants are used up."""
    if not inside_claude_code():
        return True, "outside Claude Code"
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        # an older Claude Code that does not expose the session: cannot check, do not break the user's command
        store.log_intervention({"kind": "authority_unchecked", "sub": sub, "ids": ids})
        return True, "no session id in environment"
    state = store.get_state(sid, STATE_KEY) or {}
    if time.time() - float(state.get("ts") or 0) > GRANT_TTL_S:
        return False, "no recent user prompt asked for it"
    grants = [list(g) for g in (state.get("grants") or [])]
    subs = _ALIASES.get(sub, (sub,))
    used: list[list[Any]] = []
    missing: list[str] = []
    for i in (ids or [None]):
        eid = i.upper() if i else None
        hit = next((g for g in grants if g[0] in subs and g[1] == eid), None)
        if hit is None:
            missing.append(f"{sub} {eid}" if eid else sub)
        else:
            used.append(hit)
    if missing:
        return False, "the user's latest prompt did not ask for " + ", ".join(missing)
    if consume:
        state["grants"] = [g for g in grants if g not in used]
        store.set_state(sid, STATE_KEY, state)
    return True, "granted by the user's prompt"


def refusal(sub: str, ids: list[str | None], reason: str) -> str:
    target = " ".join(i for i in ids if i) if any(ids) else ""
    example = {"add": "/ledger add --type <type> --statement \"...\" --user",
               "dedupe": "/ledger dedupe --apply --by user"}.get(sub, f"/ledger {sub} {target or '<id>'}")
    return (f"refused: user authority for `{sub}` needs the user to ask for it in this session ({reason}). "
            f"Ask the user to type `{example}` themselves.")
