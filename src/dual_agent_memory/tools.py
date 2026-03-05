"""Memory search tool interface -- callable by the main agent."""

from __future__ import annotations

from collections.abc import Iterable

from dual_agent_memory.config import Config, MemoryChunk, SearchResult
from dual_agent_memory.search import HybridSearch


class MemorySearchTool:
    """Exposes the memory archive to the main agent as a structured tool.

    Wraps :class:`HybridSearch` and returns plain dicts suitable for
    Claude tool-use responses.
    """

    def __init__(
        self,
        archive: object,
        embedder: object,
        config: Config | None = None,
    ) -> None:
        self._config = config or Config()
        self._archive = archive
        self._search = HybridSearch(archive, embedder, self._config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int | None = None,
        session_id: str | None = None,
    ) -> dict:
        """Search the memory archive for chunks relevant to *query*.

        Args:
            query: Natural-language description of what to find.
            top_k: Maximum number of results (defaults to ``Config.search_top_k``).
            session_id: If provided, restrict results to this session.

        Returns:
            A dict with ``query``, ``results`` (list of result dicts),
            and ``total_results``.
        """
        effective_k: int = top_k if top_k is not None else self._config.search_top_k

        if session_id is not None:
            results: list[SearchResult] = self._search.search_by_session(
                session_id, query, top_k=effective_k
            )
        else:
            results = self._search.search(query, top_k=effective_k)

        return {
            "query": query,
            "results": [
                self._format_result(r) for r in results
            ],
            "total_results": len(results),
        }

    def get_session_summary(self, session_id: str) -> dict:
        """Return a high-level summary of a single session.

        Aggregates topics, entities, and decisions across every chunk
        that belongs to the given session.

        Args:
            session_id: The session to summarise.

        Returns:
            A dict with ``session_id``, ``total_turns``, ``topics``,
            ``entities``, ``decisions``, and ``time_range``.
        """
        chunks: list[MemoryChunk] = self._archive.get_session_chunks(session_id)

        all_topics: list[str] = _deduplicate_ordered(
            topic for chunk in chunks for topic in chunk.topics
        )
        all_entities: list[str] = _deduplicate_ordered(
            entity for chunk in chunks for entity in chunk.entities
        )
        all_decisions: list[dict] = [
            {"decision": decision, "turn_id": chunk.turn_id}
            for chunk in chunks
            for decision in chunk.decisions
        ]

        time_range: dict = {}
        if chunks:
            time_range = {
                "start": chunks[0].timestamp,
                "end": chunks[-1].timestamp,
            }

        return {
            "session_id": session_id,
            "total_turns": len(chunks),
            "topics": all_topics,
            "entities": all_entities,
            "decisions": all_decisions,
            "time_range": time_range,
        }

    @staticmethod
    def get_tool_definition() -> dict:
        """Return the tool definition in Claude tool-use format.

        This dict can be passed directly to the ``tools`` parameter of
        the Anthropic messages API.
        """
        return {
            "name": "memory_search",
            "description": (
                "Search the conversation memory archive for relevant context "
                "from previous sessions. Use when you need to recall past "
                "decisions, check for consistency, or retrieve historical "
                "context."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Search query — describe what you're looking for"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results to return (default 5)",
                        "default": 5,
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional: limit search to a specific session"
                        ),
                    },
                },
                "required": ["query"],
            },
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_result(result: SearchResult) -> dict:
        """Convert a :class:`SearchResult` into a plain dict."""
        chunk = result.chunk
        return {
            "content": chunk.content,
            "session_id": chunk.session_id,
            "turn_id": chunk.turn_id,
            "role": chunk.role,
            "topics": chunk.topics,
            "entities": chunk.entities,
            "decisions": chunk.decisions,
            "timestamp": chunk.timestamp,
            "relevance_score": result.score,
            "match_type": result.match_type,
        }


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _deduplicate_ordered(items: Iterable[str]) -> list[str]:
    """Return a deduplicated list preserving first-occurrence order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
