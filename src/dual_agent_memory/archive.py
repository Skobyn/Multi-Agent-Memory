"""SQLite + sqlite-vec storage engine for the memory archive."""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

from dual_agent_memory.config import Config, MemoryChunk, SearchResult


def _serialize_f32(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


class MemoryArchive:
    """Core storage layer backed by SQLite with optional sqlite-vec for vector search."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._vec_available = False

        db_dir = Path(config.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(config.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._try_load_vec_extension()
        self._create_tables()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store_chunk(self, chunk: MemoryChunk) -> None:
        with self._conn:
            self._insert_chunk(chunk)

    def store_chunks(self, chunks: list[MemoryChunk]) -> None:
        with self._conn:
            for chunk in chunks:
                self._insert_chunk(chunk)

    def get_chunk(self, chunk_id: str) -> MemoryChunk | None:
        row = self._conn.execute(
            "SELECT chunk_id, session_id, turn_id, role, content, "
            "timestamp, topics, entities, decisions "
            "FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_chunk(row)

    def get_session_chunks(self, session_id: str) -> list[MemoryChunk]:
        rows = self._conn.execute(
            "SELECT chunk_id, session_id, turn_id, role, content, "
            "timestamp, topics, entities, decisions "
            "FROM chunks WHERE session_id = ? ORDER BY turn_id",
            (session_id,),
        ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_all_chunks(self) -> list[MemoryChunk]:
        rows = self._conn.execute(
            "SELECT chunk_id, session_id, turn_id, role, content, "
            "timestamp, topics, entities, decisions "
            "FROM chunks ORDER BY turn_id",
        ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_all_decisions(self) -> list[MemoryChunk]:
        rows = self._conn.execute(
            "SELECT chunk_id, session_id, turn_id, role, content, "
            "timestamp, topics, entities, decisions "
            "FROM chunks WHERE decisions != '[]' ORDER BY timestamp",
        ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def vector_search(
        self, embedding: list[float], top_k: int = 5
    ) -> list[SearchResult]:
        if not self._vec_available:
            return []

        query_blob = _serialize_f32(embedding)
        rows = self._conn.execute(
            "SELECT rowid, distance "
            "FROM chunk_vectors "
            "WHERE embedding MATCH ? "
            "ORDER BY distance "
            "LIMIT ?",
            (query_blob, top_k),
        ).fetchall()

        results: list[SearchResult] = []
        for rowid, distance in rows:
            chunk_id = self._conn.execute(
                "SELECT chunk_id FROM chunks WHERE rowid = ?", (rowid,)
            ).fetchone()
            if chunk_id is None:
                continue
            chunk = self.get_chunk(chunk_id[0])
            if chunk is None:
                continue
            # Convert L2 distance to a 0-1 similarity score.
            # distance is squared L2 for unit vectors: range [0, 4].
            score = max(0.0, 1.0 - distance / 4.0)
            results.append(SearchResult(chunk=chunk, score=score, match_type="vector"))
        return results

    def keyword_search(
        self, query: str, top_k: int = 5
    ) -> list[SearchResult]:
        rows = self._conn.execute(
            "SELECT c.chunk_id, c.session_id, c.turn_id, c.role, c.content, "
            "c.timestamp, c.topics, c.entities, c.decisions, "
            "rank "
            "FROM chunk_fts f "
            "JOIN chunks c ON c.chunk_id = f.chunk_id "
            "WHERE chunk_fts MATCH ? "
            "ORDER BY rank "
            "LIMIT ?",
            (query, top_k),
        ).fetchall()

        results: list[SearchResult] = []
        for row in rows:
            chunk = self._row_to_chunk(row[:9])
            raw_rank = row[9]
            # BM25 rank is negative (lower = more relevant). Normalise to 0-1.
            score = 1.0 / (1.0 + abs(raw_rank))
            results.append(
                SearchResult(chunk=chunk, score=score, match_type="keyword")
            )
        return results

    def count_chunks(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _try_load_vec_extension(self) -> None:
        try:
            self._conn.enable_load_extension(True)
            import sqlite_vec  # type: ignore[import-untyped]

            sqlite_vec.load(self._conn)
            self._vec_available = True
        except Exception:
            self._vec_available = False

    def _create_tables(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id   TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id    INTEGER NOT NULL,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    timestamp  TEXT NOT NULL,
                    topics     TEXT NOT NULL DEFAULT '[]',
                    entities   TEXT NOT NULL DEFAULT '[]',
                    decisions  TEXT NOT NULL DEFAULT '[]'
                )
                """
            )

            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chunks_session
                ON chunks(session_id)
                """
            )

            if self._vec_available:
                try:
                    self._conn.execute(
                        f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors
                        USING vec0(embedding float[{self._config.embedding_dim}])
                        """
                    )
                except Exception:
                    self._vec_available = False

            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts
                USING fts5(chunk_id, content)
                """
            )

    def _insert_chunk(self, chunk: MemoryChunk) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO chunks "
            "(chunk_id, session_id, turn_id, role, content, "
            "timestamp, topics, entities, decisions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk.chunk_id,
                chunk.session_id,
                chunk.turn_id,
                chunk.role,
                chunk.content,
                chunk.timestamp,
                json.dumps(chunk.topics),
                json.dumps(chunk.entities),
                json.dumps(chunk.decisions),
            ),
        )

        # FTS index — delete any prior entry then insert fresh.
        self._conn.execute(
            "DELETE FROM chunk_fts WHERE chunk_id = ?", (chunk.chunk_id,)
        )
        self._conn.execute(
            "INSERT INTO chunk_fts (chunk_id, content) VALUES (?, ?)",
            (chunk.chunk_id, chunk.content),
        )

        # Vector index
        if self._vec_available and chunk.embedding is not None:
            rowid = self._conn.execute(
                "SELECT rowid FROM chunks WHERE chunk_id = ?",
                (chunk.chunk_id,),
            ).fetchone()
            if rowid is not None:
                blob = _serialize_f32(chunk.embedding)
                self._conn.execute(
                    "INSERT OR REPLACE INTO chunk_vectors (rowid, embedding) "
                    "VALUES (?, ?)",
                    (rowid[0], blob),
                )

    @staticmethod
    def _row_to_chunk(row: tuple) -> MemoryChunk:
        return MemoryChunk(
            chunk_id=row[0],
            session_id=row[1],
            turn_id=row[2],
            role=row[3],
            content=row[4],
            timestamp=row[5],
            topics=json.loads(row[6]),
            entities=json.loads(row[7]),
            decisions=json.loads(row[8]),
        )
