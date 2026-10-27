#!/usr/bin/env python3
"""Phase 1 exit check (DESIGN.md §10): a scripted session with three compactions.

Seeds user-stated decisions and constraints into a synthetic Claude Code
transcript, runs the real hooks in-process (real extractor LLM, real
decision model), generates each compaction summary with an LLM the way a
compactor would, and measures:

  recall     seeded items that ended up in the ledger (extraction quality)
  summary    seeded items still visible in the compaction summary alone
  retained   seeded items visible in summary OR the re-injected pin block

Exit criterion: retained >= 95% and recall >= 90% after each of the three
compactions, with no tool calls required from the main agent. Usage:

  EPISTEMIC_DATA_DIR=/tmp/p1 python3 -I scripts/phase1_check.py [--out results.json]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from epistemic_monitor import hooklib  # noqa: E402
from epistemic_monitor.config import load_config  # noqa: E402
from epistemic_monitor.hooks import HANDLERS  # noqa: E402
from epistemic_monitor.llm import make_llm  # noqa: E402
from epistemic_monitor.store import LedgerStore  # noqa: E402

# (user statement, keys that identify the item in any text, type)
SEEDED = [
    ("Let's go with PostgreSQL as the primary database. Not Mongo, not anything else.", ["postgresql", "postgres"], "decision"),
    ("For auth we'll use JWT with refresh tokens.", ["jwt"], "decision"),
    ("Important: never send any email on my behalf. Draft it and show me; I send.", ["email"], "constraint"),
    ("Always run pytest before you commit anything. No exceptions.", ["pytest"], "process"),
    ("The public API of the billing module must not change; other teams depend on it.", ["billing"], "constraint"),
    ("Use Tailwind for all CSS from now on.", ["tailwind"], "decision"),
    ("Do not add new third-party dependencies without asking me first.", ["dependenc"], "constraint"),
    ("Use Redis for caching.", ["redis"], "decision"),
    ("Infra is Terraform only. No click-ops in the console.", ["terraform"], "decision"),
    ("All dates in API responses are ISO 8601 strings, UTC.", ["iso 8601", "iso-8601", "iso8601"], "constraint"),
    ("Assume Q3 data is excluded from every report because of the migration bug.", ["q3"], "premise"),
    ("Front-end tests use Vitest, not Jest.", ["vitest"], "decision"),
]

FILLER_USER = [
    "Can you look at the login page layout? The header overlaps the form on narrow screens.",
    "What does the standup summary look like? Pull it from the notes directory.",
    "Let's review the onboarding copy; marketing flagged the second paragraph.",
    "How is the sprint burn-down? I need numbers for the Thursday sync.",
    "Check the lint output please, CI went yellow on the last run.",
    "Summarize the open issues by area and say which are blocked.",
    "Rename the helper in utils so it matches the naming in the services package.",
    "What's left on the roadmap for this week, and what is at risk?",
    "Can you draft release notes for 2.4 from the merged PRs?",
    "The analytics dashboard loads slowly; profile the query behind the top chart.",
]
FILLER_ASSISTANT = [
    "I looked at src/web/pages/login.tsx. The header is position: fixed with a fixed height, and the form container "
    "uses a top margin that was tuned for desktop. I changed the container to use padding-top derived from the header "
    "height variable and verified at 360px, 768px and 1280px widths. The overlap is gone and the existing snapshot "
    "tests still pass. I also removed an unused import that the linter had been warning about for a while.",
    "The standup notes for the last three days are in notes/standup/. Monday: payment retries shipped; Tuesday: "
    "the search indexer was reworked to batch writes; Wednesday: two flaky tests were quarantined and a ticket "
    "opened to fix them properly. Open blockers are the vendor sandbox outage and the pending design review for "
    "the new settings page.",
    "I read docs/onboarding.md. The second paragraph promised a feature that is still behind a flag, so I rewrote "
    "it to describe the current behavior and moved the promise to a 'coming soon' note. I tightened two other "
    "sentences and fixed a broken link to the pricing page.",
    "Burn-down: 34 points planned, 21 done, 9 in review, 4 not started. At the current pace the sprint lands with "
    "about 3 points carried over, both from the reporting epic. I put the chart in reports/sprint-burndown.png.",
    "Lint output had 14 warnings: 9 unused variables in the test helpers, 3 import-order issues, and 2 long lines. "
    "I fixed all of them; the only non-trivial one was an unused fixture that was actually masking a missing "
    "parametrization, which I restored. CI is green again.",
    "Open issues by area: API (4, one blocked on the schema decision), web (6, none blocked), infra (2, both "
    "waiting on the vendor sandbox), docs (3). The two blocked infra issues are the oldest in the queue.",
    "Renamed utils.fmt_date to utils.format_date to match services.format_currency, updated 11 call sites, and "
    "added a deprecation shim for the old name that logs a warning. Tests pass.",
    "Left this week: finish the export endpoint, review the two reporting PRs, and write the migration guide. "
    "At risk: the migration guide, because the schema discussion is still open.",
    "Draft release notes for 2.4 are in CHANGELOG.md under Unreleased: new export endpoint, faster search "
    "indexing, login layout fix on narrow screens, and 12 bug fixes. I grouped them by area and linked the PRs.",
    "The slow chart ran a query without an index on (account_id, created_at); EXPLAIN showed a sequential scan "
    "over 4.1M rows. I added a composite index in a migration and the query dropped from 2.3 s to 40 ms locally. "
    "I did not touch the chart code.",
]
FILLERS_PER_ITEM = 4

def row(kind: str, text: str, sid: str, uuid_: str, parent: str | None, ts: float, version: str = "2.1.311") -> dict:
    content = text if kind == "user" else [{"type": "text", "text": text}]
    return {"parentUuid": parent, "isSidechain": False, "type": kind,
            "message": {"role": kind, "content": content}, "uuid": uuid_, "sessionId": sid, "version": version,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)), "cwd": str(ROOT)}


def write_segment(path: Path, sid: str, seg_items: list[tuple[str, list[str], str]], seg_index: int, last_uuid: str | None) -> str:
    rows = []
    t = time.time() + seg_index * 3600
    parent = last_uuid
    fillers_u, fillers_a = list(FILLER_USER), list(FILLER_ASSISTANT)
    for i, (statement, _keys, _t) in enumerate(seg_items):
        for _ in range(FILLERS_PER_ITEM):  # filler exchanges between seeded statements
            u, a = str(uuid.uuid4()), str(uuid.uuid4())
            rows.append(row("user", fillers_u[(i * 2 + len(rows)) % len(fillers_u)], sid, u, parent, t))
            rows.append(row("assistant", fillers_a[(i * 2 + len(rows)) % len(fillers_a)], sid, a, u, t + 5))
            parent, t = a, t + 10
        u, a = str(uuid.uuid4()), str(uuid.uuid4())
        rows.append(row("user", statement, sid, u, parent, t))
        rows.append(row("assistant", "Understood, noted. I'll proceed accordingly.", sid, a, u, t + 5))
        parent, t = a, t + 10
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return parent


def run_hook(event: str, payload: dict, cfg) -> tuple[dict | None, float]:
    out = io.StringIO()
    t0 = time.monotonic()
    hooklib.run(event, HANDLERS[event], payload={"cwd": str(ROOT), **payload}, stdout=out)
    return (json.loads(out.getvalue()) if out.getvalue() else None), time.monotonic() - t0


def compactor_summary(llm, transcript_path: Path, max_words: int) -> str:
    text = []
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        c = r["message"]["content"]
        text.append(f"{r['type']}: {c if isinstance(c, str) else c[0]['text']}")
    convo = "\n".join(text)[-60000:]
    for attempt in range(3):
        comp = llm.complete(
            "You compact an agent conversation so it can continue in a fresh context window.",
            f"Write a summary of at most {max_words} words of the conversation below, focused on what was worked on, "
            f"what was completed, and what remains. Plain prose.\n\n{convo}", max_tokens=1200)
        if comp.text.strip():
            return comp.text
        print(f"    (compactor returned an empty summary on attempt {attempt + 1}; retrying)")
    raise RuntimeError("compactor returned an empty summary three times")


def contains(text: str, keys: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keys)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--summary-words", type=int, default=150)
    ap.add_argument("--min-recall", type=float, default=0.9)
    args = ap.parse_args()
    data_dir = os.environ.get("EPISTEMIC_DATA_DIR")
    if not data_dir:
        print("set EPISTEMIC_DATA_DIR to a throwaway directory", file=sys.stderr)
        return 2
    cfg = load_config(cwd=ROOT)
    cfg.ensure_dirs()
    sid = f"phase1-{int(time.time())}"
    transcript = cfg.data_dir / f"{sid}.jsonl"
    llm = make_llm(cfg.section("extractor"))
    print(f"data dir {cfg.data_dir}\nextractor {llm.name}; decision model {cfg.get('decision_model','provider')}:{cfg.get('decision_model','model')}\n")

    segments = [SEEDED[0:4], SEEDED[4:8], SEEDED[8:12]]
    last_uuid = None
    results = {"session": sid, "compactions": [], "extractor": llm.name}
    overall_ok = True
    for k, seg in enumerate(segments, start=1):
        last_uuid = write_segment(transcript, sid, seg, k, last_uuid)
        _, t_pre = run_hook("pre_compact", {"session_id": sid, "trigger": "auto", "transcript_path": str(transcript)}, cfg)
        summary = compactor_summary(llm, transcript, args.summary_words)
        _, t_post = run_hook("post_compact", {"session_id": sid, "trigger": "auto", "compact_summary": summary}, cfg)
        out, t_start = run_hook("session_start", {"session_id": sid, "source": "compact"}, cfg)
        pinned = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        store = LedgerStore(cfg)
        try:
            ledger_text = "\n".join(e.statement for e in store.entries(None))
            audit = [r for r in store.recent_interventions(50) if r.get("kind") == "compaction_audit"][-1]
            n_entries = store.count_entries()
        finally:
            store.close()
        seeded_so_far = [s for segm in segments[:k] for s in segm]
        per_item = []
        for statement, keys, etype in seeded_so_far:
            per_item.append({"item": statement[:60], "type": etype,
                             "in_ledger": contains(ledger_text, keys), "in_summary": contains(summary, keys),
                             "in_pinned": contains(pinned, keys)})
        recall = sum(i["in_ledger"] for i in per_item) / len(per_item)
        summ = sum(i["in_summary"] for i in per_item) / len(per_item)
        retained = sum(i["in_summary"] or i["in_pinned"] for i in per_item) / len(per_item)
        ok = retained >= 0.95 and recall >= args.min_recall
        overall_ok &= ok
        results["compactions"].append({
            "k": k, "seeded": len(per_item), "recall": recall, "summary_only": summ, "retained": retained,
            "ledger_entries": n_entries, "audit_retention": audit.get("retention"), "summary_chars": len(summary),
            "dropped": audit.get("dropped"), "pinned_chars": len(pinned),
            "t_pre_compact_s": round(t_pre, 2), "t_post_compact_s": round(t_post, 2), "t_session_start_s": round(t_start, 3),
            "items": per_item, "summary": summary,
        })
        print(f"compaction {k}: seeded {len(per_item)}  recall {recall:.0%}  summary-only {summ:.0%}  "
              f"retained {retained:.0%}  ledger {n_entries} entries  audit retention "
              f"{audit.get('retention') if audit.get('retention') is None else round(audit['retention'], 2)}  "
              f"dropped {audit.get('dropped')}  summary {len(summary)} chars  pin {len(pinned)} chars  "
              f"[pre {t_pre:.1f}s, post {t_post:.1f}s, start {t_start * 1000:.0f}ms]  {'PASS' if ok else 'FAIL'}")
        for i in per_item:
            flag = "" if i["in_summary"] or i["in_pinned"] else "   <-- LOST"
            print(f"    {'L' if i['in_ledger'] else '-'}{'S' if i['in_summary'] else '-'}{'P' if i['in_pinned'] else '-'}  {i['item']}{flag}")
    results["pass"] = overall_ok
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.out}")
    print("\nRESULT:", "PASS (retained >= 95%% and recall >= %.0f%% after every compaction)" % (args.min_recall * 100) if overall_ok else "FAIL")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
