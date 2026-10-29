"""Machine-wide limits on the plugin's own model work, so a burst of sessions (subagents, teams,
several terminals) can never fan out into hundreds of extractions and `claude -p` children.

Incident 2026-10-29 (DESIGN §11 item 11): with the advisor and extractor on `claude-cli` and the plugin
loaded through CLAUDE_CODE_PLUGIN_DIRS, every extraction spawned a `claude -p` that loaded the plugin
again and ran its own hooks; with no cap, several hundred processes accumulated. Three guards:

* ``CHILD_FLAG``: set on every process the plugin starts; a hook that sees it exits at once.
* ``slot()``: at most ``extraction.max_concurrent`` model-calling workers per machine (file locks).
* a per-session debounce, ``extraction.min_interval_s``, applied by the prompt hook.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import Iterator

CHILD_FLAG = "EPISTEMIC_MONITOR_CHILD"
STRIP_FROM_CHILD_ENV = ("CLAUDE_CODE_PLUGIN_DIRS", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")


def is_child(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(CHILD_FLAG))


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for anything the plugin spawns: no plugin directories, no parent session, and the
    child flag so a nested Claude Code session (or a nested hook) does nothing."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in STRIP_FROM_CHILD_ENV}
    env[CHILD_FLAG] = "1"
    return env


def lock_dir() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return base / "epistemic" / "locks"


@contextlib.contextmanager
def slot(name: str, max_concurrent: int, directory: Path | None = None) -> Iterator[bool]:
    """Hold one of ``max_concurrent`` machine-wide slots named ``name`` for the block; yields False at once
    when every slot is busy (the caller skips its work). Locks release when the process ends, however it ends."""
    directory = directory or lock_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield True   # no lock dir: better to work than to wedge
        return
    held = None
    for i in range(max(1, int(max_concurrent))):
        try:
            fh = open(directory / f"{name}.{i}.lock", "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = fh
            break
        except OSError:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
    if held is None:
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        except OSError:
            pass
