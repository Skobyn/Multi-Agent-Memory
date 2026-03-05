"""Tests for archive storage, search, chunker, embedder, and hybrid search."""

import math

import pytest

from dual_agent_memory.archive import MemoryArchive
from dual_agent_memory.chunker import Chunker
from dual_agent_memory.config import Config, MemoryChunk
from dual_agent_memory.embedder import Embedder
from dual_agent_memory.search import HybridSearch


# -------------------------------------------------------------------------
# Archive storage tests
# -------------------------------------------------------------------------


def test_archive_create_tables(config):
    """Archive creates the chunks, FTS, and index tables on init."""
    archive = MemoryArchive(config)

    # Verify the main chunks table exists by running a query against it.
    row = archive._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
    ).fetchone()
    assert row is not None, "chunks table should exist after archive init"

    # Verify the FTS virtual table exists.
    fts_row = archive._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chunk_fts'"
    ).fetchone()
    assert fts_row is not None, "chunk_fts table should exist after archive init"

    archive.close()


def test_archive_store_and_retrieve(config, sample_chunks):
    """Store a single chunk then retrieve it by chunk_id."""
    archive = MemoryArchive(config)
    chunk = sample_chunks[0]

    archive.store_chunk(chunk)
    retrieved = archive.get_chunk(chunk.chunk_id)

    assert retrieved is not None, "stored chunk should be retrievable"
    assert retrieved.chunk_id == chunk.chunk_id, "chunk_id should match"
    assert retrieved.content == chunk.content, "content should match"
    assert retrieved.session_id == chunk.session_id, "session_id should match"
    assert retrieved.role == chunk.role, "role should match"

    archive.close()


def test_archive_store_chunks_batch(config, sample_chunks):
    """Store multiple chunks at once via store_chunks."""
    archive = MemoryArchive(config)

    archive.store_chunks(sample_chunks)
    count = archive.count_chunks()

    assert count == len(sample_chunks), (
        f"expected {len(sample_chunks)} chunks stored, got {count}"
    )

    archive.close()


def test_archive_session_chunks(config, sample_chunks):
    """get_session_chunks returns all chunks for a given session, ordered by turn_id."""
    archive = MemoryArchive(config)
    archive.store_chunks(sample_chunks)

    session_chunks = archive.get_session_chunks("test-session-1")

    assert len(session_chunks) == len(sample_chunks), (
        "session should contain all stored chunks"
    )
    turn_ids = [c.turn_id for c in session_chunks]
    assert turn_ids == sorted(turn_ids), "chunks should be ordered by turn_id"

    archive.close()


def test_archive_count(config, sample_chunks):
    """count_chunks returns the correct number of stored chunks."""
    archive = MemoryArchive(config)

    assert archive.count_chunks() == 0, "empty archive should have count 0"

    archive.store_chunks(sample_chunks)
    assert archive.count_chunks() == len(sample_chunks), (
        "count should match stored chunks"
    )

    archive.close()


def test_archive_decisions(config, sample_chunks):
    """get_all_decisions returns only chunks that have non-empty decisions."""
    archive = MemoryArchive(config)
    archive.store_chunks(sample_chunks)

    decision_chunks = archive.get_all_decisions()

    # Every returned chunk must have at least one decision.
    for chunk in decision_chunks:
        assert len(chunk.decisions) > 0, (
            f"chunk {chunk.chunk_id} returned by get_all_decisions "
            f"should have decisions but has: {chunk.decisions}"
        )

    # At least some sample messages contain decisions (the assistant ones do).
    assert len(decision_chunks) > 0, (
        "sample data should produce at least one chunk with decisions"
    )

    archive.close()


def test_archive_keyword_search(config, sample_chunks):
    """FTS5 keyword search finds relevant chunks."""
    archive = MemoryArchive(config)
    archive.store_chunks(sample_chunks)

    results = archive.keyword_search("PostgreSQL")

    assert len(results) > 0, "keyword search for 'PostgreSQL' should find results"
    # At least one result should mention PostgreSQL.
    found = any("PostgreSQL" in r.chunk.content for r in results)
    assert found, "at least one result should contain 'PostgreSQL'"

    archive.close()


def test_archive_keyword_no_results(config, sample_chunks):
    """Keyword search returns empty list for a non-matching query."""
    archive = MemoryArchive(config)
    archive.store_chunks(sample_chunks)

    results = archive.keyword_search("xylophoneUnicornRainbow")

    assert len(results) == 0, (
        "keyword search for nonsensical term should return empty list"
    )

    archive.close()


# -------------------------------------------------------------------------
# Chunker tests
# -------------------------------------------------------------------------


def test_chunker_basic(config, sample_messages):
    """chunk_messages produces one chunk per message with sequential turn_ids."""
    chunker = Chunker(config)
    chunks = chunker.chunk_messages(sample_messages, session_id="s1")

    assert len(chunks) == len(sample_messages), (
        f"expected {len(sample_messages)} chunks, got {len(chunks)}"
    )

    expected_turn_ids = list(range(1, len(sample_messages) + 1))
    actual_turn_ids = [c.turn_id for c in chunks]
    assert actual_turn_ids == expected_turn_ids, (
        f"turn_ids should be sequential starting at 1: {actual_turn_ids}"
    )


def test_chunker_extracts_topics(config, sample_messages):
    """Topics are extracted from message content."""
    chunker = Chunker(config)
    chunks = chunker.chunk_messages(sample_messages, session_id="s1")

    all_topics = []
    for chunk in chunks:
        all_topics.extend(chunk.topics)

    assert len(all_topics) > 0, "chunker should extract at least one topic"


def test_chunker_extracts_entities(config, sample_messages):
    """Named entities are extracted from message content."""
    chunker = Chunker(config)
    chunks = chunker.chunk_messages(sample_messages, session_id="s1")

    all_entities = []
    for chunk in chunks:
        all_entities.extend(chunk.entities)

    assert len(all_entities) > 0, "chunker should extract at least one entity"

    # The messages mention JWT, PostgreSQL, MongoDB, FastAPI, Flask, Redis, Memcached.
    all_entity_text = " ".join(all_entities).lower()
    # Check at least one known entity appears.
    known = ["jwt", "postgresql", "mongodb", "fastapi", "flask", "redis", "memcached"]
    found_any = any(name in all_entity_text for name in known)
    assert found_any, (
        f"expected at least one known entity in extracted entities: {all_entities}"
    )


def test_chunker_extracts_decisions(config, sample_messages):
    """Decision statements are extracted from assistant messages."""
    chunker = Chunker(config)
    chunks = chunker.chunk_messages(sample_messages, session_id="s1")

    all_decisions = []
    for chunk in chunks:
        all_decisions.extend(chunk.decisions)

    assert len(all_decisions) > 0, (
        "chunker should extract decisions from assistant messages"
    )

    # The messages contain phrases like "decided to", "chose", "going with".
    all_decision_text = " ".join(all_decisions).lower()
    decision_indicators = ["decided", "chose", "going with", "will use", "should use"]
    found_any = any(ind in all_decision_text for ind in decision_indicators)
    assert found_any, (
        f"extracted decisions should contain decision-indicating phrases: {all_decisions}"
    )


# -------------------------------------------------------------------------
# Embedder tests
# -------------------------------------------------------------------------


def test_embedder_consistent(config):
    """Same text produces the same embedding on repeated calls."""
    embedder = Embedder(config)
    text = "Use PostgreSQL for the database."

    emb1 = embedder.embed(text)
    emb2 = embedder.embed(text)

    assert emb1 == emb2, "same text should produce identical embeddings"


def test_embedder_dimension(config):
    """Embeddings have the correct dimension from config."""
    embedder = Embedder(config)
    text = "Use PostgreSQL for the database."

    emb = embedder.embed(text)

    assert len(emb) == config.embedding_dim, (
        f"embedding dimension should be {config.embedding_dim}, got {len(emb)}"
    )


def test_embedder_normalized(config):
    """Embeddings are unit length (L2 norm approximately 1.0)."""
    embedder = Embedder(config)
    text = "Use PostgreSQL for the database."

    emb = embedder.embed(text)
    norm = math.sqrt(sum(v * v for v in emb))

    assert abs(norm - 1.0) < 1e-6, (
        f"embedding should be unit-normalized, got norm={norm}"
    )


def test_embedder_different(config):
    """Different texts produce different embeddings."""
    embedder = Embedder(config)

    emb1 = embedder.embed("PostgreSQL is a relational database.")
    emb2 = embedder.embed("Redis is an in-memory cache.")

    assert emb1 != emb2, "different texts should produce different embeddings"


# -------------------------------------------------------------------------
# Hybrid search tests
# -------------------------------------------------------------------------


class _SimpleArchive:
    """Minimal archive adapter that satisfies HybridSearch's protocol
    by providing get_all_chunks from a stored list."""

    def __init__(self):
        self._chunks: list[MemoryChunk] = []

    def add_chunks(self, chunks: list[MemoryChunk]) -> None:
        self._chunks.extend(chunks)

    def get_all_chunks(self) -> list[MemoryChunk]:
        return list(self._chunks)


def _embed_chunks(chunks: list[MemoryChunk], embedder: Embedder) -> list[MemoryChunk]:
    """Attach embeddings to each chunk (in-place) and return the list."""
    for chunk in chunks:
        chunk.embedding = embedder.embed(chunk.content)
    return chunks


def test_hybrid_search(config, sample_chunks):
    """Hybrid search returns relevant results for a matching query."""
    embedder = Embedder(config)
    _embed_chunks(sample_chunks, embedder)

    archive = _SimpleArchive()
    archive.add_chunks(sample_chunks)

    search = HybridSearch(archive, embedder, config)
    results = search.search("PostgreSQL database")

    assert len(results) > 0, "hybrid search should return at least one result"

    # The top result should be related to the PostgreSQL discussion.
    top_content = results[0].chunk.content.lower()
    assert "postgresql" in top_content, (
        f"top result should mention PostgreSQL, got: {top_content[:100]}"
    )


def test_hybrid_search_ranking(config, sample_chunks):
    """More relevant results should rank higher than less relevant ones."""
    embedder = Embedder(config)
    _embed_chunks(sample_chunks, embedder)

    archive = _SimpleArchive()
    archive.add_chunks(sample_chunks)

    search = HybridSearch(archive, embedder, config)
    results = search.search("JWT authentication tokens")

    assert len(results) >= 2, "search should return at least 2 results for ranking test"

    # Scores should be in descending order.
    scores = [r.score for r in results]
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1], (
            f"results should be ranked by descending score: {scores}"
        )


def test_search_by_entity(config, sample_chunks):
    """Entity-specific search returns chunks mentioning the given entity."""
    embedder = Embedder(config)
    _embed_chunks(sample_chunks, embedder)

    archive = _SimpleArchive()
    archive.add_chunks(sample_chunks)

    search = HybridSearch(archive, embedder, config)
    results = search.search_by_entity("Redis")

    assert len(results) > 0, "entity search for 'Redis' should return results"

    # At least the top result should mention Redis.
    found = any("Redis" in r.chunk.content for r in results)
    assert found, "entity search results should contain chunks mentioning 'Redis'"
