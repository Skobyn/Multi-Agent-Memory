"""The optional MCP server: JSON-RPC over stdio, four ledger tools, agent authority on supersession."""
import json
import os
import subprocess
import sys
from pathlib import Path

from helpers import make_entry

from epistemic_monitor import mcp_server

ROOT = Path(__file__).resolve().parent.parent


def _req(method, params=None, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params is not None else {})}


def test_handle_lists_and_calls_tools(cfg, store):
    user_e = make_entry(store, statement="Use PostgreSQL as the primary database.", scope={"paths": ["src/db/*"]})
    agent_e = make_entry(store, statement="Cache sessions in Redis.", source="agent", confirmed=False)
    init = mcp_server.handle(_req("initialize", {"protocolVersion": "2025-06-18"}), cfg, store)
    assert init["result"]["serverInfo"]["name"] == "epistemic-monitor" and "tools" in init["result"]["capabilities"]
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, cfg, store) is None
    names = [t["name"] for t in mcp_server.handle(_req("tools/list"), cfg, store)["result"]["tools"]]
    assert names == ["ledger_search", "ledger_briefing", "ledger_status", "ledger_supersede"]
    r = mcp_server.handle(_req("tools/call", {"name": "ledger_search", "arguments": {"query": "postgres database"}}), cfg, store)
    assert user_e.id in r["result"]["content"][0]["text"] and not r["result"].get("isError")
    r = mcp_server.handle(_req("tools/call", {"name": "ledger_briefing", "arguments": {}}), cfg, store)
    assert "Session briefing" in r["result"]["content"][0]["text"] and agent_e.id in r["result"]["content"][0]["text"]
    r = mcp_server.handle(_req("tools/call", {"name": "ledger_status", "arguments": {}}), cfg, store)
    assert json.loads(r["result"]["content"][0]["text"])["entries"]["active"] == 2
    # agent authority: a user entry is refused, an agent entry superseded
    r = mcp_server.handle(_req("tools/call", {"name": "ledger_supersede", "arguments": {"id": user_e.id, "reason": "x"}}), cfg, store)
    assert r["result"]["isError"] and "SUPERSEDE" in r["result"]["content"][0]["text"]
    r = mcp_server.handle(_req("tools/call", {"name": "ledger_supersede", "arguments": {"id": agent_e.id, "reason": "memcached instead"}}), cfg, store)
    assert not r["result"].get("isError") and store.get_entry(agent_e.id).status == "superseded"
    r = mcp_server.handle(_req("tools/call", {"name": "nope", "arguments": {}}), cfg, store)
    assert r["result"]["isError"]
    assert mcp_server.handle(_req("bogus"), cfg, store)["error"]["code"] == -32601


def test_serve_over_pipes(cfg, store):
    make_entry(store, statement="Dates in API responses are ISO 8601 strings.")
    store.close()
    env = {**os.environ, "EPISTEMIC_DATA_DIR": str(cfg.data_dir), "PYTHONPATH": str(ROOT / "src")}
    lines = "\n".join(json.dumps(m) for m in [_req("initialize", {"protocolVersion": "2025-06-18"}, 1),
                                                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                                _req("tools/call", {"name": "ledger_search", "arguments": {"query": "ISO dates"}}, 2)]) + "\n"
    lines += "not json\n" + json.dumps("a string, not a request") + "\n"
    p = subprocess.run([sys.executable, str(ROOT / "bin" / "em-hook"), "mcp"], input=lines, capture_output=True, text=True,
                       env=env, cwd=str(cfg.cwd), timeout=60)
    out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    assert p.returncode == 0, p.stdout[-400:]
    assert [o.get("id") for o in out] == [1, 2, None, None]
    assert "ISO 8601" in out[1]["result"]["content"][0]["text"]
    assert out[2]["error"]["code"] == -32700 and out[3]["error"]["code"] == -32600
