---
name: session-memory
description: Persistent cross-session memory with automatic archiving before context compaction. Preserves full conversation history, decisions, and entity state across sessions with zero message loss.
---

# Session Memory

This skill gives Claude persistent memory across sessions. Every conversation is archived, every decision is indexed, and context compaction never destroys critical history.

## Architecture

```
Current Session (context window)
       |
       v
 memory_observe (each turn)
       |
       v
 Memory Archive (SQLite)
  +-- Chunked turns (searchable)
  +-- Vector embeddings (semantic search)
  +-- FTS5 index (keyword search)
  +-- Decision index (structured decisions)
  +-- Session metadata (timestamps, topics)
```

## Session Lifecycle

### 1. Session Start

At the beginning of every session, call `memory_briefing`:

```
memory_briefing(session_id: "current-session-id")
```

This returns:
- Prior decisions from all archived sessions
- Active topics and their last state
- Key entities and their relationships

Present the briefing naturally:
```
Welcome back. From previous sessions:
- We decided on PostgreSQL for the database (Session 3)
- JWT authentication is implemented (Session 5)
- Redis caching is still pending (Session 7)
```

### 2. During the Session

**Record every turn** by calling `memory_observe` for both user and assistant messages. This builds the real-time archive and enables contradiction detection.

**Evaluate uncertain outputs** by calling `memory_evaluate` before finalizing responses that reference prior decisions or technical choices. This returns a confidence score.

**Search when needed** by calling `memory_search` when the user asks about prior work, or when the epistemic monitor flags a gap.

### 3. Before Context Compaction

When the context window is getting full (approaching compaction), call `memory_archive` with all current messages:

```
memory_archive(
  messages: [all conversation messages],
  session_id: "current-session-id"
)
```

This ensures zero message loss. Every turn is chunked, embedded, and stored before compaction destroys it.

### 4. Session End

Archive the full session if not already done. The archive preserves:
- Every user and assistant message
- Extracted topics, entities, and decisions
- Vector embeddings for semantic search
- Full-text index for keyword search

## Search Capabilities

### Hybrid Search

Every query uses both vector similarity and keyword matching, fused with Reciprocal Rank Fusion:

```
Query: "what database did we choose?"
       |
       +-- Vector search (semantic similarity)
       |   Finds: turns about databases, storage, data layers
       |
       +-- Keyword search (FTS5)
       |   Finds: turns containing "database", "chose", "PostgreSQL"
       |
       v
 RRF fusion (k=60) --> ranked results with scores
```

### Search by Entity

The archive indexes entities (people, technologies, projects) extracted from each turn. Search surfaces all mentions of an entity across sessions.

### Search by Session

Filter results to a specific session when you need the full context of a particular conversation.

## Decision Tracking

Decisions are automatically extracted from conversation turns using rule-based patterns:
- "We decided to..."
- "Let's go with..."
- "The choice is..."
- "Approved: ..."

Decisions are indexed and surfaced in:
- Session briefings
- Contradiction detection (compared against new outputs)
- Search results (tagged as decisions)

## Self-Compaction

The memory system manages its own context overhead:

1. When observed turns exceed 50, the oldest half is compacted
2. A structured summary (decisions, entities, topics) is accumulated
3. Compacted turns are archived -- nothing is lost
4. The summary prepends context before recent turns

This keeps the monitoring overhead bounded while preserving full history in the searchable archive.

## Cost Awareness

The memory system tracks token usage:
- Observation: ~100-200 tokens per turn
- Evaluation: ~200-400 tokens per check
- Search: ~100-300 tokens per query
- Briefing: ~300-500 tokens per session start

Typical overhead is 15-25% on top of the main agent's token usage when using a fast model (Haiku) for monitoring.
