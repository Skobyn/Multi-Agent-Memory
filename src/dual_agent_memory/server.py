"""MCP server exposing dual-agent memory as tools for Claude Code."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

from dual_agent_memory.archive import MemoryArchive
from dual_agent_memory.chunker import Chunker
from dual_agent_memory.config import Config
from dual_agent_memory.embedder import Embedder
from dual_agent_memory.hooks import CompactionHook
from dual_agent_memory.monitor import MemoryAgent
from dual_agent_memory.tools import MemorySearchTool


class MemoryMCPServer:
    """MCP server that wraps the dual-agent memory system."""

    def __init__(self, db_path: str | None = None) -> None:
        config = Config(db_path=db_path or os.environ.get(
            "MEMORY_DB_PATH",
            str(Path.home() / ".dual_agent_memory" / "memory.db"),
        ))
        self._config = config
        self._archive = MemoryArchive(config)
        self._embedder = Embedder(config)
        self._chunker = Chunker(config)
        self._monitor = MemoryAgent(self._archive, self._embedder, config)
        self._search_tool = MemorySearchTool(self._archive, self._embedder, config)
        self._compaction_hook = CompactionHook(
            self._archive, self._chunker, self._embedder, config
        )
        self._session_id = os.environ.get("MEMORY_SESSION_ID", "default")

    def get_tools(self) -> list[dict]:
        """Return tool definitions in MCP format."""
        return [
            {
                "name": "memory_search",
                "description": (
                    "Search the conversation memory archive for relevant context "
                    "from previous sessions. Use when you need to recall past "
                    "decisions, verify consistency, or retrieve historical context."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for in the memory archive",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Max results to return (default 5)",
                            "default": 5,
                        },
                        "session_id": {
                            "type": "string",
                            "description": "Optional: limit search to a specific session",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memory_observe",
                "description": (
                    "Record a conversation turn for the memory monitor. Call this "
                    "for each user and assistant message so the epistemic monitor "
                    "can track context and detect contradictions."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "role": {
                            "type": "string",
                            "enum": ["user", "assistant", "system"],
                            "description": "Who sent the message",
                        },
                        "content": {
                            "type": "string",
                            "description": "The message content",
                        },
                    },
                    "required": ["role", "content"],
                },
            },
            {
                "name": "memory_evaluate",
                "description": (
                    "Evaluate an assistant output against the memory archive. "
                    "Returns a confidence score (0.0-1.0) and any contradiction "
                    "or gap flags. Use before finalizing important decisions."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "output_text": {
                            "type": "string",
                            "description": "The assistant output to evaluate",
                        },
                    },
                    "required": ["output_text"],
                },
            },
            {
                "name": "memory_briefing",
                "description": (
                    "Get a session-start briefing with prior decisions, active "
                    "topics, and entity state from the archive. Call at the "
                    "beginning of a new session."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Current session identifier",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "memory_archive",
                "description": (
                    "Archive conversation messages to the memory store. Use "
                    "before context compaction or at session end to preserve "
                    "the full conversation history."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "messages": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                                "required": ["role", "content"],
                            },
                            "description": "Conversation messages to archive",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "Session identifier for these messages",
                        },
                    },
                    "required": ["messages", "session_id"],
                },
            },
            {
                "name": "memory_status",
                "description": (
                    "Get the current status of the memory system including "
                    "archive size, cost tracking, and monitor state."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def handle_tool_call(self, name: str, arguments: dict) -> dict:
        """Execute a tool and return the result."""
        # Implement each tool handler:

        if name == "memory_search":
            return self._search_tool.search(
                query=arguments["query"],
                top_k=arguments.get("top_k"),
                session_id=arguments.get("session_id"),
            )

        elif name == "memory_observe":
            signal, interrupt = self._monitor.process_turn(
                role=arguments["role"],
                content=arguments["content"],
            )
            result = {"observed": True, "role": arguments["role"]}
            if signal:
                result["confidence"] = {
                    "score": signal.score,
                    "flags": signal.flags,
                    "recommendation": signal.recommendation,
                }
                if signal.citations:
                    result["confidence"]["citations"] = signal.citations
            if interrupt:
                result["interrupt"] = {
                    "severity": interrupt.severity,
                    "message": interrupt.message,
                    "suggested_query": interrupt.suggested_query,
                }
            return result

        elif name == "memory_evaluate":
            signal = self._monitor.evaluate(arguments["output_text"])
            interrupt = self._monitor.check_interrupt(signal)
            result = {
                "score": signal.score,
                "flags": signal.flags,
                "recommendation": signal.recommendation,
                "citations": signal.citations,
            }
            if interrupt:
                result["interrupt"] = {
                    "severity": interrupt.severity,
                    "message": interrupt.message,
                    "suggested_query": interrupt.suggested_query,
                }
            return result

        elif name == "memory_briefing":
            briefing = self._monitor.generate_briefing(
                arguments["session_id"]
            )
            return {"briefing": briefing, "session_id": arguments["session_id"]}

        elif name == "memory_archive":
            stats = self._compaction_hook.before_compaction(
                messages=arguments["messages"],
                session_id=arguments["session_id"],
            )
            return stats

        elif name == "memory_status":
            cost_summary = self._monitor.cost_tracker.get_summary()
            return {
                "archive_chunks": self._archive.count_chunks(),
                "observed_turns": len(self._monitor._observed_turns),
                "has_rolling_summary": bool(self._monitor._rolling_summary),
                "session_id": self._session_id,
                "cost": cost_summary,
            }

        else:
            raise ValueError(f"Unknown tool: {name}")

    # ---------------------------------------------------------------
    # MCP JSON-RPC transport
    # ---------------------------------------------------------------

    def _read_message(self) -> dict | None:
        """Read a JSON-RPC message from stdin using Content-Length framing."""
        headers = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None  # EOF
            line_str = line.decode("utf-8").rstrip("\r\n")
            if line_str == "":
                break  # end of headers
            if ":" in line_str:
                key, value = line_str.split(":", 1)
                headers[key.strip()] = value.strip()

        content_length = int(headers.get("Content-Length", "0"))
        if content_length == 0:
            return None

        body = sys.stdin.buffer.read(content_length)
        return json.loads(body.decode("utf-8"))

    def _write_message(self, msg: dict) -> None:
        """Write a JSON-RPC message to stdout with Content-Length framing."""
        body = json.dumps(msg)
        body_bytes = body.encode("utf-8")
        header = f"Content-Length: {len(body_bytes)}\r\n\r\n"
        sys.stdout.buffer.write(header.encode("utf-8"))
        sys.stdout.buffer.write(body_bytes)
        sys.stdout.buffer.flush()

    def _handle_request(self, msg: dict) -> dict | None:
        """Handle an incoming JSON-RPC request and return response (or None for notifications)."""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        # Notifications (no id) don't get responses
        if msg_id is None:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "dual-agent-memory",
                        "version": "0.1.0",
                    },
                },
            }

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self.get_tools()},
            }

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            try:
                result = self.handle_tool_call(tool_name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(result, indent=2)}
                        ]
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": f"Error: {exc}"}
                        ],
                        "isError": True,
                    },
                }

        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }

    def run(self) -> None:
        """Run the MCP server main loop (stdin/stdout)."""
        while True:
            msg = self._read_message()
            if msg is None:
                break
            response = self._handle_request(msg)
            if response is not None:
                self._write_message(response)


def main() -> None:
    """Entry point for the MCP server."""
    import argparse
    parser = argparse.ArgumentParser(description="Dual-Agent Memory MCP Server")
    parser.add_argument("--db-path", help="Path to SQLite database")
    parser.add_argument("--session-id", help="Session identifier")
    args = parser.parse_args()

    if args.session_id:
        os.environ["MEMORY_SESSION_ID"] = args.session_id

    server = MemoryMCPServer(db_path=args.db_path)
    server.run()


if __name__ == "__main__":
    main()
