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
    restated: dict[str, int] = defaultdict(int)
    confirmed: set[str] = set()
    for r in interventions:
        if r.get("kind") == "gate":
            for eid in r.get("flagged") or []:
                flags[eid] += 1
                if label_by_tool.get(r.get("tool_use_id")) == "ok":
                    ok_labeled[eid] += 1
        elif r.get("kind") == "supersede" and r.get("by") == "user" and r.get("ok", True):
            superseded_by_user.add(r.get("entry"))
        elif r.get("kind") == "dedup_discarded_user_claim" and r.get("evidence_supported") is not False:
            restated[r.get("entry")] += 1
        elif r.get("kind") == "entry_confirmed":
            confirmed.add(r.get("entry"))
    review = [{"entry": e, "flags": n} for e, n in sorted(flags.items(), key=lambda kv: -kv[1])
              if n >= min_flags and e in superseded_by_user]
    tighten = [{"entry": e, "flags": flags[e], "labeled_ok": n, "ok_share": round(n / flags[e], 2)}
               for e, n in sorted(ok_labeled.items(), key=lambda kv: -kv[1]) if n / flags[e] >= 0.5]
    # an advisory entry the extractor later saw the user state again: never promoted automatically (the overlap
    # test passes on questions and quotes), only offered for `ledger confirm`
    confirm = [{"entry": e, "restated": n} for e, n in sorted(restated.items(), key=lambda kv: -kv[1])
               if e not in confirmed and e not in superseded_by_user]
    return {"candidates_for_review": review, "tighten": tighten, "confirm_candidates": confirm}
