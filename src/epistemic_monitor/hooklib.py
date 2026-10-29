"""Shared plumbing for hook entry points: stdin JSON in, stdout JSON out,
never crash the session, always leave a log line."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from typing import Any, Callable

from .config import Config, load_config
from .store import LedgerStore
from .throttle import is_child

HOOK_EVENT_NAMES = {
    "session_start": "SessionStart",
    "session_end": "SessionEnd",
    "user_prompt_submit": "UserPromptSubmit",
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "post_tool_batch": "PostToolBatch",
    "stop": "Stop",
    "pre_compact": "PreCompact",
    "post_compact": "PostCompact",
    "subagent_start": "SubagentStart",
    "subagent_stop": "SubagentStop",
}

Handler = Callable[[dict[str, Any], Config, LedgerStore], dict[str, Any] | None]


def read_payload(stream=None) -> dict[str, Any]:
    stream = stream or sys.stdin
    try:
        raw = stream.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def additional_context(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT_NAMES[event], "additionalContext": text[:10000]}}


def deny(event: str, reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT_NAMES[event],
                                   "permissionDecision": "deny", "permissionDecisionReason": reason[:10000]}}


def block(reason: str) -> dict[str, Any]:
    return {"decision": "block", "reason": reason[:10000]}


def _install_term_handler() -> None:
    """When Claude Code kills a hook at its timeout, take the hook's own children (a `claude -p` call) with it
    instead of leaving them to launchd."""
    def _on_term(signum, frame):  # noqa: ANN001
        try:
            from .llm import kill_children
            kill_children()
        finally:
            os._exit(0)
    try:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGHUP, _on_term)
    except (ValueError, OSError):
        pass


def run(event: str, handler: Handler, payload: dict[str, Any] | None = None, stdout=None) -> int:
    """Execute a hook handler with full error containment. Exit code is always 0:
    the monitor must never be the reason a session fails."""
    out_stream = stdout or sys.stdout
    if is_child():
        # A session the plugin itself started (the claude-cli provider, a background worker): never run the
        # loop inside the loop. Read nothing, log nothing, allow everything.
        return 0
    _install_term_handler()
    payload = read_payload() if payload is None else payload
    t0 = time.monotonic()
    cfg: Config | None = None
    store: LedgerStore | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    try:
        cfg = load_config(payload.get("cwd"))
        store = LedgerStore(cfg)
        output = handler(payload, cfg, store)
    except Exception:  # noqa: BLE001 - containment by design
        error = traceback.format_exc()[-2000:]
    finally:
        ms = int((time.monotonic() - t0) * 1000)
        if store is not None:
            try:
                store.log_intervention({
                    "kind": "hook", "event": event, "session_id": payload.get("session_id", ""),
                    "duration_ms": ms, "ok": error is None, "error": error,
                    "emitted": sorted(output.keys()) if output else [],
                })
            except Exception:  # noqa: BLE001
                pass
            store.close()
        elif error:
            sys.stderr.write(f"epistemic-monitor {event}: {error}\n")
    if output:
        out_stream.write(json.dumps(output))
        out_stream.flush()
    return 0
