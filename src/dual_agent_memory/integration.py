"""High-level integration API for the dual-agent memory system."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dual_agent_memory.archive import MemoryArchive
from dual_agent_memory.chunker import Chunker
from dual_agent_memory.config import Config, ConfidenceSignal, Interrupt
from dual_agent_memory.embedder import Embedder
from dual_agent_memory.hooks import CompactionHook
from dual_agent_memory.monitor import MemoryAgent
from dual_agent_memory.tools import MemorySearchTool


class DualAgentMemory:
    """Single entry point for integrating dual-agent memory into any agent framework.

    Usage:
        memory = DualAgentMemory("./my_project_memory.db")

        # At session start
        briefing = memory.start_session("session-42")

        # During conversation
        result = memory.on_user_message("What database should we use?")
        result = memory.on_assistant_message("Let's use PostgreSQL.")
        if result["interrupt"]:
            print(result["interrupt"]["message"])

        # Search archive
        results = memory.search("database decisions")

        # At session end
        memory.end_session(all_messages)
    """

    def __init__(
        self,
        db_path: str | None = None,
        session_id: str | None = None,
        embedding_fn=None,  # Optional: custom embedding function
    ) -> None:
        self._config = Config(
            db_path=db_path or os.environ.get(
                "MEMORY_DB_PATH",
                str(Path.home() / ".dual_agent_memory" / "memory.db"),
            )
        )
        self._session_id = session_id or "default"
        self._archive = MemoryArchive(self._config)
        self._embedder = Embedder(self._config, embed_fn=embedding_fn)
        self._chunker = Chunker(self._config)
        self._monitor = MemoryAgent(self._archive, self._embedder, self._config)
        self._search_tool = MemorySearchTool(self._archive, self._embedder, self._config)
        self._compaction_hook = CompactionHook(
            self._archive, self._chunker, self._embedder, self._config
        )

    # ----- Session lifecycle -----

    def start_session(self, session_id: str | None = None) -> dict:
        """Start a new session. Returns a briefing from prior sessions.

        Args:
            session_id: Identifier for this session. Auto-generated if not provided.

        Returns:
            dict with "session_id" and "briefing" (str) containing prior
            decisions, topics, and entities from the archive.
        """
        if session_id:
            self._session_id = session_id
        briefing = self._monitor.generate_briefing(self._session_id)
        return {
            "session_id": self._session_id,
            "briefing": briefing,
            "archive_size": self._archive.count_chunks(),
        }

    def end_session(self, messages: list[dict] | None = None) -> dict:
        """End the current session. Optionally archive all messages.

        Args:
            messages: Full conversation to archive. If None, only observed
                     turns are archived.

        Returns:
            dict with archival stats and cost summary.
        """
        stats = {}
        if messages:
            stats = self._compaction_hook.before_compaction(
                messages, self._session_id
            )
        consolidation = self._compaction_hook.run_consolidation(self._session_id)
        cost = self._monitor.cost_tracker.get_summary()
        return {
            "archive_stats": stats,
            "consolidation": consolidation,
            "cost_summary": cost,
        }

    # ----- Conversation flow -----

    def on_user_message(self, content: str) -> dict:
        """Process a user message. Records it for monitoring.

        Args:
            content: The user's message text.

        Returns:
            dict with "role" and "observed" keys.
        """
        self._monitor.observe("user", content)
        return {"role": "user", "observed": True}

    def on_assistant_message(self, content: str) -> dict:
        """Process an assistant message. Evaluates against archive.

        This is the main integration point. Call this with every assistant
        output to get real-time contradiction and gap detection.

        Args:
            content: The assistant's output text.

        Returns:
            dict with:
            - "confidence": {"score": float, "flags": list, "recommendation": str}
            - "interrupt": None or {"severity": str, "message": str, "suggested_query": str}
        """
        signal, interrupt = self._monitor.process_turn("assistant", content)

        result: dict[str, Any] = {"role": "assistant", "observed": True}

        if signal:
            result["confidence"] = {
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
        else:
            result["interrupt"] = None

        return result

    # ----- Search and retrieval -----

    def search(self, query: str, top_k: int = 5, session_id: str | None = None) -> dict:
        """Search the memory archive.

        Args:
            query: Natural language search query.
            top_k: Maximum number of results.
            session_id: Optional session filter.

        Returns:
            dict with "query", "results" (list), and "total_results".
        """
        return self._search_tool.search(query, top_k=top_k, session_id=session_id)

    def get_session_summary(self, session_id: str | None = None) -> dict:
        """Get a summary of a specific session.

        Args:
            session_id: Session to summarize. Defaults to current session.

        Returns:
            dict with topics, entities, decisions, and time range.
        """
        sid = session_id or self._session_id
        return self._search_tool.get_session_summary(sid)

    # ----- Direct archive operations -----

    def archive_messages(self, messages: list[dict], session_id: str | None = None) -> dict:
        """Archive conversation messages (e.g., before compaction).

        Args:
            messages: List of {"role": str, "content": str} dicts.
            session_id: Session identifier. Defaults to current session.

        Returns:
            Archival stats dict.
        """
        sid = session_id or self._session_id
        return self._compaction_hook.before_compaction(messages, sid)

    def consolidate(self, session_id: str | None = None) -> dict:
        """Run decision consolidation on the archive.

        Marks older superseded decisions. Preserves audit trail.

        Args:
            session_id: Optional session filter.

        Returns:
            Consolidation stats dict.
        """
        return self._compaction_hook.run_consolidation(session_id)

    # ----- Tool definition for agent frameworks -----

    def get_tool_definitions(self) -> list[dict]:
        """Return tool definitions compatible with Claude/OpenAI tool-use format.

        Returns a list of tool dicts that can be passed to an LLM's tool
        parameter. Includes memory_search and memory_session_summary.
        """
        return [
            MemorySearchTool.get_tool_definition(),
            {
                "name": "memory_session_summary",
                "description": (
                    "Get a summary of a previous conversation session including "
                    "decisions made, topics discussed, and entities mentioned."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The session ID to summarize",
                        },
                    },
                    "required": ["session_id"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, arguments: dict) -> dict:
        """Handle a tool call from the agent framework.

        Args:
            tool_name: Name of the tool being called.
            arguments: Tool arguments dict.

        Returns:
            Tool result dict.
        """
        if tool_name == "memory_search":
            return self.search(
                query=arguments["query"],
                top_k=arguments.get("top_k", 5),
                session_id=arguments.get("session_id"),
            )
        elif tool_name == "memory_session_summary":
            return self.get_session_summary(arguments["session_id"])
        else:
            return {"error": f"Unknown tool: {tool_name}"}

    # ----- Status and cost -----

    @property
    def cost_tracker(self):
        """Access the cost tracker for token usage reporting."""
        return self._monitor.cost_tracker

    def status(self) -> dict:
        """Get current system status."""
        return {
            "session_id": self._session_id,
            "archive_chunks": self._archive.count_chunks(),
            "observed_turns": len(self._monitor._observed_turns),
            "has_rolling_summary": bool(self._monitor._rolling_summary),
            "cost": self._monitor.cost_tracker.get_summary(),
        }

    def close(self) -> None:
        """Close the archive database connection."""
        self._archive.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
