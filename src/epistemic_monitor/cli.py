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
    s_int = sub.add_parser("interventions"); s_int.add_argument("n", nargs="?", type=int, default=20)
    sub.add_parser("status")
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
        elif args.cmd == "interventions":
            for r in store.recent_interventions(args.n):
                print(json.dumps(r, ensure_ascii=False)[:400])
        elif args.cmd == "status":
            print(json.dumps(_status_dict(cfg, store), indent=2))
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
    cfg = load_config()
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
            res = _extract_one_capture(cfg, store, capture, str(info.get("session_id", "unknown")), args.budget)
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
    cfg = load_config()
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
    if cmd == "hook":
        if not rest or rest[0] not in HANDLERS:
            sys.stderr.write(f"unknown hook event; expected one of {', '.join(HANDLERS)}\n")
            return 0  # never fail the session
        return run(rest[0], HANDLERS[rest[0]])
    sys.stderr.write(f"unknown command {cmd!r}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
