"""User-authority grants for the ledger CLI (docs/RESEARCH-2026-11-17.md, I1).

The agent has Bash, and `/ledger` runs the CLI through the agent, so `ledger confirm`, `add --user` and
`--by user` would otherwise grant user authority on the agent's say-so. A misled agent (MemoryDojo: injected
text that persists and steers; HANDBOOK.md: a plausible in-environment request overriding standing policy) runs
exactly that command. So when the CLI runs inside Claude Code it asks for a grant: the user's latest real prompt
in this session must be a typed `/ledger` command naming the same subcommand and target (`/ledger confirm
D-0007`). Prose does not grant. Grants are recorded at UserPromptSubmit, expire, and are used once.

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
_ID_RE = re.compile(r"^([DCPKR]-\d{4,})$", re.IGNORECASE)
_BY_USER_RE = re.compile(r"--by(?:=|\s+)user\b")
# A typed slash command, plugin-namespaced or not: "/ledger confirm D-0007", "/epistemic-monitor:ledger add ...".
# Only typed commands grant. Prose does not: "confirm D-0007 still holds" asks for a check, and a misled agent
# is told precisely to read such text as a request (review of 2026-11-17).
_SLASH_RE = re.compile(r"^\s*/(?:[\w.-]+:)?ledger\s+(" + "|".join(_SUBS) + r")\b([^\n]*)", re.IGNORECASE | re.MULTILINE)


def grant_sub(sub: str, keep: bool | None = None) -> str:
    """`resolve --keep` and `resolve --retire` are opposite actions: a grant names which one."""
    if sub == "resolve":
        return "resolve-keep" if keep else "resolve-retire"
    return sub


def ledger_grants(prompt: str) -> list[list[Any]]:
    """[subcommand, entry id or scope] pairs the user's own typed /ledger commands authorize."""
    grants: list[list[Any]] = []

    def add(sub: str, target: str | None) -> None:
        g = [sub, target]
        if g not in grants:
            grants.append(g)

    for m in _SLASH_RE.finditer(prompt or ""):
        sub, rest = m.group(1).lower(), m.group(2)
        words = rest.split()
        # only positional ids before the first flag: "--reason 'see D-0008'" names no target
        positional = []
        for w in words:
            if w.startswith("--"):
                break
            positional.append(w)
        ids = [w.upper() for w in positional if _ID_RE.match(w)]
        if sub == "add":
            if re.search(r"(?:^|\s)--user\b", rest):
                add("add", None)
        elif sub == "dedupe":
            if "--apply" in words and _BY_USER_RE.search(rest):
                add("dedupe", None)
        elif sub == "resolve":
            keep = "--keep" in words
            if keep or "--retire" in words:
                for eid in ids:
                    add(grant_sub("resolve", keep), eid)
        elif sub == "retire" and not ids:
            pm = re.search(r"--provenance(?:=|\s+)(agent|user)\b", rest)
            if pm:
                add("retire", "provenance:" + pm.group(1))
        else:
            for eid in ids:
                add(sub, eid)
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
    outcome: dict[str, Any] = {}

    def take(state: Any) -> Any:
        state = state or {}
        if time.time() - float(state.get("ts") or 0) > GRANT_TTL_S:
            outcome["reason"] = "no recent user prompt asked for it"
            return state
        grants = [list(g) for g in (state.get("grants") or [])]
        want = [[sub, (i.upper() if i and not i.startswith("provenance:") else i)] for i in (ids or [None])]
        missing = [w for w in want if w not in grants]
        if missing:
            outcome["reason"] = "the user's latest prompt did not ask for " + ", ".join(
                f"{s_} {i}" if i else s_ for s_, i in missing)
            return state
        outcome["ok"] = True
        if consume:
            state["grants"] = [g for g in grants if g not in want]
        return state

    # read and spend in one write transaction: two parallel Bash calls cannot both use a single-use grant
    store.update_state(sid, STATE_KEY, take)
    if not outcome.get("ok"):
        return False, outcome.get("reason", "not granted")
    return True, "granted by the user's prompt"


def refusal(sub: str, ids: list[str | None], reason: str) -> str:
    target = " ".join(i for i in ids if i and not i.startswith("provenance:")) or "<id>"
    prov = next((i.split(":", 1)[1] for i in ids if i and i.startswith("provenance:")), None)
    example = {"add": "/ledger add --type <type> --statement \"...\" --user",
               "dedupe": "/ledger dedupe --apply --by user",
               "resolve-keep": f"/ledger resolve {target} --keep --by user",
               "resolve-retire": f"/ledger resolve {target} --retire --by user",
               "supersede": f"/ledger supersede {target} --reason \"...\" --by user",
               "retire": (f"/ledger retire --provenance {prov} --reason \"...\" --by user" if prov
                          else f"/ledger retire {target} --reason \"...\" --by user"),
               }.get(sub, f"/ledger {sub} {target}")
    return (f"refused: user authority for `{sub}` needs the user to ask for it in this session ({reason}). "
            f"Ask the user to type `{example}` themselves.")
