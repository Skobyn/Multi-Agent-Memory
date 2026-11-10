"""Read-only gate-feedback report (idea from REALM, arXiv 2609.16053, narrowed).

Aggregates gate flags per ledger entry and joins them to later user supersessions and explicit
``ledger label`` verdicts. It never writes events or changes status: a flag followed by a
supersession is ambiguous (the user may simply be unblocking), so a human decides.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def feedback_report(interventions: list[dict[str, Any]], labels: list[dict[str, Any]] | None = None,
                    min_flags: int = 3) -> dict[str, list[dict[str, Any]]]:
    flags: dict[str, int] = defaultdict(int)
    ok_labeled: dict[str, int] = defaultdict(int)
    label_by_tool = {l.get("tool_use_id"): l.get("label") for l in (labels or [])}
    superseded_by_user: set[str] = set()
    for r in interventions:
        if r.get("kind") == "gate":
            for eid in r.get("flagged") or []:
                flags[eid] += 1
                if label_by_tool.get(r.get("tool_use_id")) == "ok":
                    ok_labeled[eid] += 1
        elif r.get("kind") == "supersede" and r.get("by") == "user" and r.get("ok", True):
            superseded_by_user.add(r.get("entry"))
    review = [{"entry": e, "flags": n} for e, n in sorted(flags.items(), key=lambda kv: -kv[1])
              if n >= min_flags and e in superseded_by_user]
    tighten = [{"entry": e, "flags": flags[e], "labeled_ok": n, "ok_share": round(n / flags[e], 2)}
               for e, n in sorted(ok_labeled.items(), key=lambda kv: -kv[1]) if n / flags[e] >= 0.5]
    return {"candidates_for_review": review, "tighten": tighten}


def why_report(interventions: list[dict[str, Any]], tool_use_id: str, get_entry) -> dict[str, Any] | None:
    """Trace one gate decision back to the entries that drove it (forensic join, no replay).

    ``get_entry`` maps an id to an Entry or None. Entries are shown in their *current* state, with the
    supersede/retire records that followed the decision, so a user can see where a bad deny came from.
    """
    gate = next((r for r in reversed(interventions) if r.get("kind") == "gate" and r.get("tool_use_id") == tool_use_id), None)
    if gate is None:
        return None
    ids: list[str] = []
    for key in ("flagged", "conflicts", "scoped_entries", "candidates"):
        for eid in gate.get(key) or []:
            if isinstance(eid, str) and eid not in ids:
                ids.append(eid)
    ts = str(gate.get("ts") or "")
    rows = []
    for eid in ids:
        e = get_entry(eid)
        # The entry itself records when it was closed (valid_to) and by what; intervention logs are not complete for this.
        later = []
        if e is not None and e.valid_to and e.valid_to > ts:
            later.append({"kind": e.status, "by": e.superseded_by or "", "ts": e.valid_to})
        sc = (gate.get("scores") or {}).get(eid) or {}
        rows.append({"entry": eid, "flagged": eid in (gate.get("flagged") or []), "p_conflict": sc.get("p_conflict"),
                     "status": e.status if e else "missing", "statement": e.statement if e else "",
                     "source": e.provenance.source if e else "", "session_id": e.provenance.session_id if e else "",
                     "turn_uuid": e.provenance.turn_uuid if e else "", "later": later})
    return {"tool_use_id": tool_use_id, "decision": gate.get("decision"), "tool": gate.get("tool"),
            "path": gate.get("path"), "ts": ts, "entries": rows}
