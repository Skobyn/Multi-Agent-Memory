"""Command-line entry used by hooks.json, the /ledger command, and background workers.

    em hook <event>                    (stdin: Claude Code hook JSON)
    em status
    em ledger list [--all] | show <id> | add ... | supersede <id> ... | pin | interventions [n] | status
    em extract-capture <transcript.jsonl[.gz]> --session <id>
    em extract-pending                 (retry captures whose extraction never finished)
    em extract-text <file> --session <id> --role user|assistant
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .config import load_config
from .hooklib import run
from .hooks import HANDLERS, write_pinned
from .schema import ENTRY_TYPES, Entry, Provenance, Scope
from .store import LedgerStore
from .transcript import KIND_ASSISTANT_TEXT, KIND_USER_TEXT, Turn, read_turns


def _status_dict(cfg, store) -> dict:
    counts = {s: len(store.entries(s)) for s in ("active", "superseded", "orphaned", "unknown_current", "retired")}
    recent = store.recent_interventions(10)
    return {
        "version": __version__, "data_dir": str(cfg.data_dir), "entries": counts,
        "decision_model": f"{cfg.get('decision_model', 'provider')}:{cfg.get('decision_model', 'model')}",
        "extractor": f"{cfg.get('extractor', 'provider')}:{cfg.get('extractor', 'model')}",
        "advisor": f"{cfg.get('advisor', 'provider')}:{cfg.get('advisor', 'model')}",
        "gate_enforce": cfg.get("gate", "enforce"), "recent_events": [r.get("kind") for r in recent],
        "pinned": cfg.pinned_path.exists(),
    }


def status(argv: list[str]) -> int:
    cfg = load_config()
    store = LedgerStore(cfg)
    try:
        print(json.dumps(_status_dict(cfg, store), indent=2))
    finally:
        store.close()
    return 0


def _fmt_entry(e: Entry, verbose: bool = False) -> str:
    prov = e.provenance.source + (" ✓" if e.provenance.confirmed_by_user else "")
    line = f"{e.id}  [{e.type}, {prov}, {e.status}]  {e.statement}"
    if not verbose:
        return line
    parts = [line]
    if e.rationale:
        parts.append(f"    rationale: {e.rationale}")
    if e.scope.paths or e.scope.tools or e.scope.topics:
        parts.append(f"    scope: paths={e.scope.paths} tools={e.scope.tools} topics={e.scope.topics}")
    if e.falsifiers:
        parts.append(f"    falsifiers: {json.dumps(e.falsifiers)}")
    if e.anchors:
        parts.append(f"    anchors: {', '.join(e.anchors[:12])}")
    if e.depends_on or e.supersedes or e.superseded_by:
        parts.append(f"    depends_on={e.depends_on} supersedes={e.supersedes} superseded_by={e.superseded_by}")
    parts.append(f"    recorded {e.provenance.timestamp} in session {e.provenance.session_id or '?'}"
                 f" (confidence {e.provenance.extraction_confidence:.2f})")
    return "\n".join(parts)


def ledger(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="em ledger")
    sub = p.add_subparsers(dest="cmd")
    s_list = sub.add_parser("list"); s_list.add_argument("--all", action="store_true")
    s_show = sub.add_parser("show"); s_show.add_argument("id")
    s_add = sub.add_parser("add")
    s_add.add_argument("--type", required=True, choices=ENTRY_TYPES)
    s_add.add_argument("--statement", required=True)
    s_add.add_argument("--rationale", default="")
    s_add.add_argument("--path", action="append", default=[])
    s_add.add_argument("--user", action="store_true", help="the user asked for this entry (hard enforcement)")
    s_sup = sub.add_parser("supersede"); s_sup.add_argument("id"); s_sup.add_argument("--reason", required=True)
    s_sup.add_argument("--by", choices=("user", "agent"), default="agent")
    sub.add_parser("pin")
    sub.add_parser("graph", help="dependency and supersession edges, with the entries that need review")
    s_dd = sub.add_parser("dedupe", help="find active entries that say the same thing; --apply keeps the earliest and supersedes the rest")
    s_dd.add_argument("--apply", action="store_true"); s_dd.add_argument("--by", choices=("user", "agent"), default="agent")
    s_dd.add_argument("--jaccard", type=float, default=0.8)
    s_rt = sub.add_parser("retire", help="close entries without a replacement: by id, or every entry of a provenance")
    s_rt.add_argument("ids", nargs="*"); s_rt.add_argument("--provenance", choices=("agent", "user"), default=None)
    s_rt.add_argument("--reason", required=True); s_rt.add_argument("--by", choices=("user", "agent"), default="agent")
    s_res = sub.add_parser("resolve", help="close a review item (orphaned or unknown_current)")
    s_res.add_argument("id"); g = s_res.add_mutually_exclusive_group(required=True)
    g.add_argument("--keep", action="store_true"); g.add_argument("--retire", action="store_true")
    s_int = sub.add_parser("interventions"); s_int.add_argument("n", nargs="?", type=int, default=20)
    sub.add_parser("status")
    sub.add_parser("doctor", help="one real call per configured component: keys, decision model, advisor, extractor, embeddings")
    s_rev = sub.add_parser("review", help="list recent gate decisions that involved the decision model, for labeling")
    s_rev.add_argument("n", nargs="?", type=int, default=20)
    s_lab = sub.add_parser("label", help="record whether a flagged tool call really conflicted")
    s_lab.add_argument("tool_use_id"); s_lab.add_argument("label", choices=("conflict", "ok"))
    s_good = sub.add_parser("mark-good", help="mark a session as known-good for the false-deny quantile")
    s_good.add_argument("session_id")
    s_cal = sub.add_parser("calibrate"); s_cal.add_argument("--max-false-deny", type=float, default=0.02)
    s_cal.add_argument("--alpha", type=float, default=0.05); s_cal.add_argument("--min-labels", type=int, default=12)
    args = p.parse_args(argv or ["list"])
    cfg = load_config()
    store = LedgerStore(cfg)
    try:
        if args.cmd == "list":
            entries = store.entries(None) if args.all else store.active_entries()
            if not entries:
                print("(ledger is empty)" if args.all else "(no active entries)")
            for e in entries:
                print(_fmt_entry(e))
        elif args.cmd == "show":
            e = store.get_entry(args.id.upper())
            print(_fmt_entry(e, verbose=True) if e else f"no entry {args.id}")
        elif args.cmd == "add":
            source = "user" if args.user else "agent"
            e = Entry(id=store.next_id(args.type), type=args.type, statement=args.statement, rationale=args.rationale,
                      scope=Scope(paths=args.path),
                      provenance=Provenance(source=source, confirmed_by_user=args.user, session_id="cli", extraction_confidence=1.0))
            from .anchors import extract_anchors
            e.anchors = extract_anchors(e.statement, e.rationale, *args.path)
            store.add_entry(e)
            store.log_intervention({"kind": "ledger_add", "entry": e.id, "source": source})
            write_pinned(cfg, store)
            print(_fmt_entry(e, verbose=True))
        elif args.cmd == "supersede":
            ok = store.supersede(args.id.upper(), None, args.reason, by=args.by, session_id="cli")
            write_pinned(cfg, store)
            print(f"superseded {args.id.upper()}" if ok else
                  f"refused: {args.id.upper()} is a user decision; only the user may supersede it (use --by user if the user asked)")
        elif args.cmd == "pin":
            print(write_pinned(cfg, store))
        elif args.cmd == "graph":
            g = store.graph()
            by_id = {n["id"]: n for n in g["nodes"]}
            for n in g["nodes"]:
                print(f"{n['id']}  [{n['type']}, {n['source']}, {n['status']}]  {n['statement']}")
            for ed in g["edges"]:
                print(f"    {ed['from']} --{ed['kind']}--> {ed['to']}  ({by_id.get(ed['to'], {}).get('status', '?')})")
            review = store.needs_review()
            print(f"\nneeds review: {len(review)}" + ("" if not review else "  (resolve with `ledger resolve <id> --keep|--retire`)"))
            for e in review:
                print(f"  {e.id}  [{e.status}]  {e.statement}")
        elif args.cmd == "dedupe":
            groups = store.duplicate_groups(args.jaccard)
            if not groups:
                print("(no duplicates among active entries)")
            for g in groups:
                keep, rest = g[0], g[1:]
                print(f"{keep.id}  keep   {keep.statement[:110]}")
                for e in rest:
                    if args.apply:
                        ok = store.supersede(e.id, keep.id, f"duplicate of {keep.id}", by=args.by, session_id="cli")
                        print(f"  {e.id}  {'superseded' if ok else 'refused (user entry; use --by user)'}  {e.statement[:100]}")
                    else:
                        print(f"  {e.id}  dup    {e.statement[:100]}")
            if args.apply and groups:
                write_pinned(cfg, store)
            elif groups:
                print(f"\n{sum(len(g) - 1 for g in groups)} would be superseded; rerun with --apply")
        elif args.cmd == "retire":
            ids = [i.upper() for i in args.ids]
            if args.provenance:
                ids += [e.id for e in store.active_entries() if e.provenance.source == args.provenance and e.id not in ids]
            if not ids:
                print("nothing to retire: give ids or --provenance agent|user")
            n = 0
            for eid in ids:
                ok = store.retire(eid, args.reason, by=args.by, session_id="cli")
                n += ok
                print(f"{eid}  {'retired' if ok else 'refused or not active'}")
            if n:
                write_pinned(cfg, store)
            print(f"retired {n}")
        elif args.cmd == "resolve":
            ok = store.resolve(args.id.upper(), keep=bool(args.keep))
            write_pinned(cfg, store)
            print(("kept " if args.keep else "retired ") + args.id.upper() if ok else f"{args.id.upper()} is not under review")
        elif args.cmd == "interventions":
            for r in store.recent_interventions(args.n):
                print(json.dumps(r, ensure_ascii=False)[:400])
        elif args.cmd == "status":
            print(json.dumps(_status_dict(cfg, store), indent=2))
        elif args.cmd == "doctor":
            from .doctor import main as doctor_main
            return doctor_main([])
        elif args.cmd == "review":
            gates = [r for r in store.recent_interventions(2000) if r.get("kind") == "gate" and r.get("scores")][-args.n:]
            if not gates:
                print("(no gate decisions with model scores yet)")
            for r in gates:
                print(f"{r.get('tool_use_id')}  {r.get('decision'):8} {r.get('tool'):10} {r.get('path') or ''}  "
                      f"max_p={r.get('max_p_conflict')}  flagged={r.get('flagged')}  conflicts={r.get('conflicts')}")
                for eid, sc in (r.get("scores") or {}).items():
                    print(f"      {eid}: p={sc.get('p_conflict')} sev={sc.get('severity')} tau={sc.get('tau')}")
            print("\nlabel with: em ledger label <tool_use_id> conflict|ok")
        elif args.cmd == "label":
            from .calibration import add_label
            rec = next((r for r in store.recent_interventions(5000) if r.get("kind") == "gate" and r.get("tool_use_id") == args.tool_use_id), None)
            if rec is None:
                print(f"no gate record with tool_use_id {args.tool_use_id}")
            else:
                scores = {eid: sc.get("p_conflict") for eid, sc in (rec.get("scores") or {}).items() if isinstance(sc.get("p_conflict"), (int, float))}
                types = {}
                for eid in scores:
                    e = store.get_entry(eid)
                    if e is not None:
                        types[eid] = e.type
                add_label(cfg, {"tool_use_id": args.tool_use_id, "label": args.label, "scores": scores, "types": types,
                                "session_id": rec.get("session_id"), "decision": rec.get("decision")})
                print(f"labeled {args.tool_use_id} as {args.label} ({len(scores)} scored entries)")
        elif args.cmd == "mark-good":
            from .calibration import mark_good_session
            mark_good_session(cfg, args.session_id)
            print(f"session {args.session_id} marked known-good")
        elif args.cmd == "calibrate":
            from .calibration import calibrate
            gates = [r for r in store.recent_interventions(20000) if r.get("kind") == "gate"]
            cal = calibrate(cfg, gates, args.max_false_deny, args.alpha, args.min_labels)
            print(json.dumps(cal.to_dict(), indent=2))
    finally:
        store.close()
    return 0


def _extract_one_capture(cfg, store, path: str, session_id: str, budget: float) -> dict:
    from .hooks import _extractor_for
    turns, stats = read_turns(path, after_uuid=store.get_watermark(session_id))
    ex = _extractor_for(cfg, store, budget)
    res = ex.run(turns, session_id, mode="full").to_dict() if ex and turns else {"skipped": "no extractor or no turns"}
    ok = ex is not None and not res.get("errors")
    if ok and stats.last_uuid:
        store.set_watermark(session_id, stats.last_uuid)
    if res.get("added"):
        write_pinned(cfg, store)
    store.log_intervention({"kind": "background_extraction", "source": "capture", "session_id": session_id,
                            "file": path, "new_turns": len(turns), "extraction": res, "complete": ok})
    res["complete"] = ok
    return res


def extract_capture(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="em extract-capture")
    p.add_argument("path"); p.add_argument("--session", required=True)
    p.add_argument("--budget", type=float, default=120.0)
    args = p.parse_args(argv)
    cfg = load_config()
    from .throttle import slot
    with slot("extraction", int(cfg.get("extraction", "max_concurrent", 2))) as got:
        if not got:
            print(json.dumps({"skipped": "busy"}))
            return 0
        store = LedgerStore(cfg)
        try:
            print(json.dumps(_extract_one_capture(cfg, store, args.path, args.session, args.budget)))
        finally:
            store.close()
    return 0


def extract_pending(argv: list[str]) -> int:
    """Process every pending capture marker; remove a marker only when its extraction completed
    without errors, so a network outage leaves it for the next SessionStart."""
    p = argparse.ArgumentParser(prog="em extract-pending")
    p.add_argument("--budget", type=float, default=120.0)
    args = p.parse_args(argv)
    from .hooks import pending_dir
    from .throttle import slot
    cfg = load_config()
    with slot("extraction", int(cfg.get("extraction", "max_concurrent", 2))) as got:
        if not got:
            print(json.dumps({"skipped": "busy"}))   # markers stay; the next SessionStart retries
            return 0
        return _extract_pending_locked(cfg, pending_dir, args.budget)


def _extract_pending_locked(cfg, pending_dir, budget: float) -> int:  # noqa: ANN001
    store = LedgerStore(cfg)
    done, left = [], []
    try:
        for marker in sorted(pending_dir(cfg).glob("*.json")):
            try:
                info = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError:
                marker.unlink(missing_ok=True)
                continue
            capture = info.get("capture", "")
            if not Path(capture).exists():
                marker.unlink(missing_ok=True)
                continue
            res = _extract_one_capture(cfg, store, capture, str(info.get("session_id", "unknown")), budget)
            if res.get("complete"):
                marker.unlink(missing_ok=True)
                done.append(capture)
            else:
                left.append(capture)
        store.log_intervention({"kind": "pending_extraction", "done": len(done), "left": len(left)})
        print(json.dumps({"done": done, "left": left}))
    finally:
        store.close()
    return 0


def extract_text(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="em extract-text")
    p.add_argument("path"); p.add_argument("--session", required=True)
    p.add_argument("--role", choices=("user", "assistant"), default="assistant")
    p.add_argument("--budget", type=float, default=60.0)
    args = p.parse_args(argv)
    from .hooks import _extractor_for
    from .throttle import slot
    cfg = load_config()
    with slot("extraction", int(cfg.get("extraction", "max_concurrent", 2))) as got:
        if not got:
            print(json.dumps({"skipped": "busy"}))   # the text file stays for a later run
            return 0
        store = LedgerStore(cfg)
        try:
            text = Path(args.path).read_text(encoding="utf-8", errors="replace")
            kind = KIND_USER_TEXT if args.role == "user" else KIND_ASSISTANT_TEXT
            ex = _extractor_for(cfg, store, args.budget)
            res = ex.run([Turn(uuid="", kind=kind, role=args.role, text=text)], args.session, mode="incremental").to_dict() \
                if ex else {"skipped": "no extractor"}
            if res.get("added"):
                write_pinned(cfg, store)
            store.log_intervention({"kind": "background_extraction", "source": f"stop:{args.role}", "session_id": args.session,
                                    "chars": len(text), "extraction": res})
            try:
                Path(args.path).unlink()
            except OSError:
                pass
            print(json.dumps(res))
        finally:
            store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "version":
        print(__version__)
        return 0
    if cmd == "status":
        return status(rest)
    if cmd == "ledger":
        return ledger(rest)
    if cmd == "extract-capture":
        return extract_capture(rest)
    if cmd == "extract-pending":
        return extract_pending(rest)
    if cmd == "extract-text":
        return extract_text(rest)
    if cmd == "mcp":
        from .mcp_server import serve
        return serve()
    if cmd == "doctor":
        from .doctor import main as doctor_main
        return doctor_main(rest)
    if cmd == "hook":
        if not rest or rest[0] not in HANDLERS:
            sys.stderr.write(f"unknown hook event; expected one of {', '.join(HANDLERS)}\n")
            return 0  # never fail the session
        return run(rest[0], HANDLERS[rest[0]])
    sys.stderr.write(f"unknown command {cmd!r}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
