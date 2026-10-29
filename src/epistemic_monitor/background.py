"""Detached background work for hooks with tight budgets (SessionEnd, Stop).

The worker is this same package invoked through ``bin/em-hook`` in a new
session so the hook can return immediately. Output goes to
``<data_dir>/background.log``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import Config

ROOT = Path(__file__).resolve().parents[2]
EM_HOOK = ROOT / "bin" / "em-hook"


def spawn(cfg: Config, args: list[str]) -> int | None:
    """Start ``em-hook <args...>`` detached. Returns the pid, or None on failure."""
    try:
        cfg.ensure_dirs()
        log = open(cfg.data_dir / "background.log", "ab")
        from .throttle import child_env
        env = child_env()   # the worker calls models but must never be mistaken for a session's hook
        env["EPISTEMIC_DATA_DIR"] = str(cfg.data_dir)
        env["CLAUDE_PROJECT_DIR"] = str(cfg.cwd)
        proc = subprocess.Popen([sys.executable, "-I", str(EM_HOOK), *args], stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=env,
                                cwd=str(cfg.cwd) if cfg.cwd.exists() else None)
        return proc.pid
    except Exception:  # noqa: BLE001 - background work is best effort
        return None
