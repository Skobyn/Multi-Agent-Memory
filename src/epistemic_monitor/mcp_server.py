"""Optional MCP server (DESIGN §1): the ledger as tools for the agent and the user.

stdio JSON-RPC, standard library only. The hooks never need it; it is a convenience for asking the
ledger questions mid-session (`ledger_search`, `ledger_briefing`, `ledger_status`) and for recording an
explicit supersession (`ledger_supersede`, agent authority: a user-provenance entry is refused, as at Stop).

Run: python3 -I bin/em-hook mcp   (the plugin's .mcp.json does this)
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .config import load_config
from .pinning import render_briefing
from .store import LedgerStore

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {"name": "ledger_search", "description": "Search the project's decision ledger (active entries) by keywords.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}},
                     "required": ["query"]}},
    {"name": "ledger_briefing", "description": "The active decisions, constraints, premises, commitments and process steps "
                                               "recorded for this project, plus entries that need review.",
     "inputSchema": {"type": "object", "properties": {"max_entries": {"type": "integer", "default": 25}}}},
    {"name": "ledger_status", "description": "Entry counts by status, configured models, enforcement state, recent events.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ledger_supersede", "description": "Record that an entry no longer holds, with a reason. Agent authority: an entry "
                                                "the user made is refused; ask the user to say `SUPERSEDE <id>: <reason>` instead.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                     "required": ["id", "reason"]}},
]


def _text(s: str, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": s}]}
    if is_error:
        out["isError"] = True
    return out


def call_tool(name: str, args: dict[str, Any], cfg, store: LedgerStore) -> dict[str, Any]:
    if name == "ledger_search":
        q = str(args.get("query", "")).strip()
        if not q:
            return _text("query is required", True)
        hits = store.search_keyword(q, top_k=int(args.get("limit", 8) or 8))
        if not hits:
            return _text("no matching active entries")
        return _text("\n".join(f"{e.id} [{e.type}, {e.provenance.source}{' ✓' if e.provenance.confirmed_by_user else ''}, {e.status}]: "
                               f"{e.statement}" + (f" (scope: {', '.join(e.scope.paths)})" if e.scope.paths else "")
                               for e, _ in hits if e.status == "active"))
    if name == "ledger_briefing":
        active = store.active_entries()
        if not active:
            return _text("The ledger has no active entries for this project.")
        return _text(render_briefing(active, store.needs_review(), int(args.get("max_entries", 25) or 25), 9000))
    if name == "ledger_status":
        from .cli import _status_dict
        return _text(json.dumps(_status_dict(cfg, store), indent=2))
    if name == "ledger_supersede":
        eid, reason = str(args.get("id", "")).upper().strip(), str(args.get("reason", "")).strip()
        if not eid or not reason:
            return _text("id and reason are required", True)
        if store.get_entry(eid) is None:
            return _text(f"no entry {eid}", True)
        ok = store.supersede(eid, None, reason, by="agent", session_id="mcp")
        if ok:
            from .hooks import write_pinned
            write_pinned(cfg, store)
            return _text(f"superseded {eid}: {reason}")
        return _text(f"refused: {eid} was recorded from the user; only the user can supersede it. Ask them to reply "
                     f"`SUPERSEDE {eid}: <reason>`.", True)
    return _text(f"unknown tool {name}", True)


def handle(msg: dict[str, Any], cfg, store: LedgerStore) -> dict[str, Any] | None:
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                                                      "serverInfo": {"name": "epistemic-monitor", "version": __version__}}}
    if method == "notifications/initialized" or (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = call_tool(str(params.get("name")), dict(params.get("arguments") or {}), cfg, store)
        except Exception as e:  # noqa: BLE001
            result = _text(f"error: {e}", True)
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    cfg = load_config()
    cfg.ensure_dirs()
    store = LedgerStore(cfg)
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
                stdout.flush()
                continue
            if not isinstance(msg, dict):
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}) + "\n")
                stdout.flush()
                continue
            reply = handle(msg, cfg, store)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()
    finally:
        store.close()
    return 0
