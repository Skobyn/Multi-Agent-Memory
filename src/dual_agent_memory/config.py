"""Shared data structures and configuration for dual-agent memory system."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class MemoryChunk:
    """A single chunk of archived conversation memory."""

    chunk_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    turn_id: int = 0
    role: str = ""  # "user" | "assistant" | "system"
    content: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    topics: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    embedding: list[float] | None = None

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "topics": self.topics,
            "entities": self.entities,
            "decisions": self.decisions,
        }


@dataclass
class SearchResult:
    """A ranked result from hybrid search."""

    chunk: MemoryChunk
    score: float  # 0.0-1.0 relevance
    match_type: str  # "vector" | "keyword" | "hybrid"


@dataclass
class ConfidenceSignal:
    """Per-output confidence signal from the Memory Agent."""

    score: float  # 0.0-1.0
    flags: list[str] = field(default_factory=list)  # "contradiction" | "gap" | "drift"
    citations: list[dict] = field(default_factory=list)
    recommendation: str = ""


@dataclass
class Interrupt:
    """Structured interrupt from Memory Agent to Main Agent."""

    severity: str  # "soft_nudge" | "hard_interrupt"
    message: str = ""
    contradicting_chunks: list[MemoryChunk] = field(default_factory=list)
    suggested_query: str = ""


@dataclass
class Config:
    """System configuration."""

    db_path: str = field(
        default_factory=lambda: os.environ.get(
            "MEMORY_DB_PATH", str(Path.home() / ".dual_agent_memory" / "memory.db")
        )
    )
    embedding_dim: int = 384  # dimension for embeddings
    soft_nudge_threshold: float = 0.7
    hard_interrupt_threshold: float = 0.5
    search_top_k: int = 5
    max_chunk_tokens: int = 512
    keyword_weight: float = 0.3  # weight for keyword score in hybrid search
    vector_weight: float = 0.7  # weight for vector score in hybrid search
    max_observed_turns: int = 50  # max turns before self-compaction
