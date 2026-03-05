"""Hybrid search combining vector similarity and keyword matching."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from dual_agent_memory.config import Config, MemoryChunk, SearchResult

if TYPE_CHECKING:
    from dual_agent_memory.embedder import Embedder


# ---------------------------------------------------------------------------
# Protocol for the archive dependency (duck-typed interface)
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryArchive(Protocol):
    """Minimal interface the archive must satisfy for search to work."""

    def get_all_chunks(self) -> list[MemoryChunk]: ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RRF_K: int = 60  # Reciprocal Rank Fusion constant


# ---------------------------------------------------------------------------
# HybridSearch
# ---------------------------------------------------------------------------


class HybridSearch:
    """Hybrid search engine that fuses vector similarity and keyword matching."""

    def __init__(self, archive: MemoryArchive, embedder: Embedder, config: Config) -> None:
        self._archive = archive
        self._embedder = embedder
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        """Run hybrid search combining vector and keyword results.

        Args:
            query: Natural language search query.
            top_k: Maximum number of results to return. Defaults to config.search_top_k.

        Returns:
            List of SearchResult sorted by combined score descending.
        """
        k = top_k if top_k is not None else self._config.search_top_k
        chunks = self._archive.get_all_chunks()
        if not chunks:
            return []

        vector_ranked = self._vector_search(query, chunks)
        keyword_ranked = self._keyword_search(query, chunks)

        return self._fuse_results(vector_ranked, keyword_ranked, k)

    def search_by_entity(self, entity: str, top_k: int | None = None) -> list[SearchResult]:
        """Search specifically for chunks mentioning a given entity.

        Args:
            entity: Entity name to search for.
            top_k: Maximum number of results. Defaults to config.search_top_k.

        Returns:
            List of SearchResult containing the entity, scored by relevance.
        """
        k = top_k if top_k is not None else self._config.search_top_k
        chunks = self._archive.get_all_chunks()
        if not chunks:
            return []

        keyword_ranked = self._keyword_search(entity, chunks)

        results: list[SearchResult] = []
        for rank, (chunk, _raw_score) in enumerate(keyword_ranked, start=1):
            rrf_score = 1.0 / (rank + _RRF_K)
            results.append(
                SearchResult(chunk=chunk, score=rrf_score, match_type="keyword")
            )

        return results[:k]

    def search_by_session(
        self, session_id: str, query: str, top_k: int | None = None
    ) -> list[SearchResult]:
        """Search within a specific session only.

        Args:
            session_id: Session to restrict search to.
            query: Natural language search query.
            top_k: Maximum number of results. Defaults to config.search_top_k.

        Returns:
            List of SearchResult from the given session, sorted by score.
        """
        all_results = self.search(query, top_k=None)
        filtered = [r for r in all_results if r.chunk.session_id == session_id]
        k = top_k if top_k is not None else self._config.search_top_k
        return filtered[:k]

    # ------------------------------------------------------------------
    # Vector search
    # ------------------------------------------------------------------

    def _vector_search(
        self, query: str, chunks: list[MemoryChunk]
    ) -> list[tuple[MemoryChunk, float]]:
        """Rank chunks by cosine similarity to the query embedding.

        Returns:
            List of (chunk, similarity) pairs sorted descending by similarity.
        """
        query_embedding = self._embedder.embed(query)

        scored: list[tuple[MemoryChunk, float]] = []
        for chunk in chunks:
            if chunk.embedding is None:
                continue
            sim = self._cosine_similarity(query_embedding, chunk.embedding)
            scored.append((chunk, sim))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Keyword search
    # ------------------------------------------------------------------

    def _keyword_search(
        self, query: str, chunks: list[MemoryChunk]
    ) -> list[tuple[MemoryChunk, float]]:
        """Rank chunks by keyword overlap with the query.

        Uses a simple TF-based scoring: count how many query terms appear in the
        chunk content (case-insensitive), normalized by query length.

        Returns:
            List of (chunk, score) pairs sorted descending, filtered to score > 0.
        """
        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        scored: list[tuple[MemoryChunk, float]] = []
        for chunk in chunks:
            content_lower = chunk.content.lower()
            # Count matching terms (including frequency in content).
            hits = 0
            for term in query_terms:
                if term in content_lower:
                    hits += 1
            # Also check metadata fields.
            meta_text = " ".join(chunk.topics + chunk.entities).lower()
            for term in query_terms:
                if term in meta_text:
                    hits += 0.5

            if hits > 0:
                score = hits / len(query_terms)
                scored.append((chunk, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Reciprocal Rank Fusion
    # ------------------------------------------------------------------

    def _fuse_results(
        self,
        vector_ranked: list[tuple[MemoryChunk, float]],
        keyword_ranked: list[tuple[MemoryChunk, float]],
        top_k: int,
    ) -> list[SearchResult]:
        """Combine vector and keyword rankings using weighted reciprocal rank fusion.

        For each result:
            score = vector_weight * (1 / (vector_rank + k)) + keyword_weight * (1 / (keyword_rank + k))

        Args:
            vector_ranked: Chunks ranked by vector similarity.
            keyword_ranked: Chunks ranked by keyword score.
            top_k: Maximum results to return.

        Returns:
            Fused and deduplicated list of SearchResult.
        """
        vector_weight = self._config.vector_weight
        keyword_weight = self._config.keyword_weight

        # Build rank maps: chunk_id -> rank_position (1-based).
        vector_ranks: dict[str, int] = {}
        for rank, (chunk, _) in enumerate(vector_ranked, start=1):
            vector_ranks[chunk.chunk_id] = rank

        keyword_ranks: dict[str, int] = {}
        for rank, (chunk, _) in enumerate(keyword_ranked, start=1):
            keyword_ranks[chunk.chunk_id] = rank

        # Collect all unique chunk_ids and their chunks.
        chunk_map: dict[str, MemoryChunk] = {}
        for chunk, _ in vector_ranked:
            chunk_map[chunk.chunk_id] = chunk
        for chunk, _ in keyword_ranked:
            chunk_map[chunk.chunk_id] = chunk

        # Compute fused scores.
        fused: dict[str, float] = {}
        match_types: dict[str, str] = {}

        for chunk_id, chunk in chunk_map.items():
            v_rank = vector_ranks.get(chunk_id)
            k_rank = keyword_ranks.get(chunk_id)

            score = 0.0
            if v_rank is not None:
                score += vector_weight * (1.0 / (v_rank + _RRF_K))
            if k_rank is not None:
                score += keyword_weight * (1.0 / (k_rank + _RRF_K))

            fused[chunk_id] = score

            if v_rank is not None and k_rank is not None:
                match_types[chunk_id] = "hybrid"
            elif v_rank is not None:
                match_types[chunk_id] = "vector"
            else:
                match_types[chunk_id] = "keyword"

        # Sort by fused score descending.
        sorted_ids = sorted(fused, key=lambda cid: fused[cid], reverse=True)

        results: list[SearchResult] = []
        for chunk_id in sorted_ids[:top_k]:
            results.append(
                SearchResult(
                    chunk=chunk_map[chunk_id],
                    score=fused[chunk_id],
                    match_type=match_types[chunk_id],
                )
            )

        return results

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a < 1e-12 or norm_b < 1e-12:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Split text into lowercase tokens, filtering short/stop words."""
        _STOP_WORDS = frozenset({
            "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "shall", "can", "to", "of", "in", "on",
            "at", "by", "up", "it", "its", "i", "me", "my", "we", "our",
            "you", "your", "he", "she", "they", "them", "and", "or", "but",
            "if", "not", "no", "so", "as", "with", "for", "from", "that",
            "this", "what", "which", "who", "how", "when", "where", "why",
        })
        words = re.findall(r"[a-zA-Z0-9]+", text.lower())
        return [w for w in words if len(w) > 1 and w not in _STOP_WORDS]
