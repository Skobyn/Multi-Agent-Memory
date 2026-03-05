"""Before-compaction hook: captures full conversation history before context compaction."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from dual_agent_memory.config import Config, MemoryChunk

logger = logging.getLogger(__name__)


class CompactionHook:
    """Intercepts context compaction to archive the full conversation into memory chunks.

    Designed to run synchronously before the compaction event and complete quickly
    (target <5 seconds for 200 turns).  Errors are caught and logged so that
    compaction is never blocked.
    """

    def __init__(
        self,
        archive: object,
        chunker: object,
        embedder: object,
        config: Config | None = None,
    ) -> None:
        # Typed loosely to avoid circular imports; the real types are
        # MemoryArchive, Chunker, and Embedder respectively.
        self._archive = archive  # .store_chunks(), .store_chunk(), .count_chunks(), .get_all_decisions()
        self._chunker = chunker  # .chunk_messages()
        self._embedder = embedder  # .embed()
        self._config = config or Config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def before_compaction(
        self, messages: list[dict], session_id: str
    ) -> dict:
        """Capture the full conversation history before context is compacted.

        Steps:
            1. Record the current archive size.
            2. Chunk every message via the chunker.
            3. Generate an embedding for each chunk.
            4. Store all chunks in the archive.
            5. Return bookkeeping stats.

        Args:
            messages: Raw conversation messages (each has "role" and "content").
            session_id: Identifier for the current session.

        Returns:
            A stats dict describing what was archived.  On error the dict
            will include an ``"error"`` key but will still be returned so
            that the caller can proceed with compaction.
        """
        stats: dict = {
            "messages_captured": len(messages),
            "chunks_created": 0,
            "chunks_before": 0,
            "chunks_after": 0,
            "session_id": session_id,
        }

        try:
            t0 = time.monotonic()

            # 1. Snapshot current size.
            count_before: int = self._archive.count_chunks()
            stats["chunks_before"] = count_before

            # 2. Chunk all messages.
            chunks: list[MemoryChunk] = self._chunker.chunk_messages(
                messages, session_id
            )
            stats["chunks_created"] = len(chunks)

            # 3. Generate embeddings.
            for chunk in chunks:
                chunk.embedding = self._embedder.embed(chunk.content)

            # 4. Persist.
            self._archive.store_chunks(chunks)

            # 5. Final count.
            count_after: int = self._archive.count_chunks()
            stats["chunks_after"] = count_after

            elapsed = time.monotonic() - t0
            stats["elapsed_seconds"] = round(elapsed, 3)
            logger.info(
                "before_compaction: archived %d messages (%d chunks) in %.3fs "
                "[session=%s]",
                len(messages),
                len(chunks),
                elapsed,
                session_id,
            )

        except Exception:
            logger.exception(
                "before_compaction failed for session %s", session_id
            )
            stats["error"] = "before_compaction encountered an exception (see logs)"

        return stats

    def run_consolidation(
        self, session_id: str | None = None
    ) -> dict:
        """Consolidate archived decisions by marking older duplicates as superseded.

        For each group of decisions that share overlapping topics, only the
        newest decision remains active; older ones are prepended with a
        ``[SUPERSEDED by <chunk_id>]`` marker.

        Args:
            session_id: If given, restrict consolidation to chunks from this
                session.  Otherwise all decision-bearing chunks are considered.

        Returns:
            A stats dict with ``topics_consolidated``,
            ``decisions_superseded``, and ``decisions_active`` counts.
        """
        stats: dict = {
            "topics_consolidated": 0,
            "decisions_superseded": 0,
            "decisions_active": 0,
        }

        try:
            # Fetch decision chunks.
            all_decision_chunks: list[MemoryChunk] = (
                self._archive.get_all_decisions()
            )

            if session_id is not None:
                all_decision_chunks = [
                    c for c in all_decision_chunks if c.session_id == session_id
                ]

            if not all_decision_chunks:
                return stats

            # Group chunks by normalised topic.  A chunk may appear in
            # multiple topic groups.
            topic_groups: dict[str, list[MemoryChunk]] = defaultdict(list)
            for chunk in all_decision_chunks:
                if chunk.topics:
                    for topic in chunk.topics:
                        topic_groups[topic.lower()].append(chunk)
                else:
                    # Chunks without topics are placed under a sentinel key
                    # so they can still be compared among themselves.
                    topic_groups["__no_topic__"].append(chunk)

            superseded_ids: set[str] = set()

            for topic, group in topic_groups.items():
                if len(group) < 2:
                    continue

                # Sort by timestamp ascending so the last element is newest.
                group.sort(key=lambda c: c.timestamp)
                newest = group[-1]

                for older_chunk in group[:-1]:
                    if older_chunk.chunk_id in superseded_ids:
                        continue  # already marked
                    if older_chunk.content.startswith("[SUPERSEDED"):
                        continue  # previously superseded

                    older_chunk.content = (
                        f"[SUPERSEDED by {newest.chunk_id}] "
                        + older_chunk.content
                    )
                    self._archive.store_chunk(older_chunk)
                    superseded_ids.add(older_chunk.chunk_id)

                stats["topics_consolidated"] += 1

            stats["decisions_superseded"] = len(superseded_ids)
            stats["decisions_active"] = len(
                {
                    c.chunk_id
                    for c in all_decision_chunks
                    if c.chunk_id not in superseded_ids
                }
            )

        except Exception:
            logger.exception("run_consolidation failed")
            stats["error"] = "run_consolidation encountered an exception (see logs)"

        return stats
