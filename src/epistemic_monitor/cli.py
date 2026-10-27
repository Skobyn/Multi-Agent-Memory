"""Command-line entry: ``em hook <event>`` (used by hooks.json) and ``em status``."""

from __future__ import annotations

import json
import sys

from . import __version__
from .config import load_config
from .hooklib import run
from .hooks import HANDLERS
from .store import LedgerStore


def status(argv: list[str]) -> int:
    cfg = load_config()
    store = LedgerStore(cfg)
    try:
        counts = {s: len(store.entries(s)) for s in ("active", "superseded", "orphaned", "unknown_current", "retired")}
        recent = store.recent_interventions(10)
        out = {
            "version": __version__, "data_dir": str(cfg.data_dir), "entries": counts,
            "decision_model": f"{cfg.get('decision_model', 'provider')}:{cfg.get('decision_model', 'model')}",
            "extractor": cfg.get("extractor", "model"), "advisor": cfg.get("advisor", "model"),
            "gate_enforce": cfg.get("gate", "enforce"), "recent_events": [r.get("kind") for r in recent],
            "pinned": cfg.pinned_path.exists(),
        }
    finally:
        store.close()
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: em hook <event> | em status | em version")
        return 0
    cmd = argv[0]
    if cmd == "version":
        print(__version__)
        return 0
    if cmd == "status":
        return status(argv[1:])
    if cmd == "hook":
        if len(argv) < 2 or argv[1] not in HANDLERS:
            sys.stderr.write(f"unknown hook event; expected one of {', '.join(HANDLERS)}\n")
            return 0  # never fail the session
        return run(argv[1], HANDLERS[argv[1]])
    sys.stderr.write(f"unknown command {cmd!r}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
