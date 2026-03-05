---
description: Search the memory archive for past conversations and decisions
argument-hint: "<query>"
---

# /memory:search

> See [CONNECTORS.md](../CONNECTORS.md) for required setup.

Search the persistent memory archive using hybrid vector + keyword search. Finds relevant past conversations, decisions, and context.

## How It Works

```
User: /memory:search authentication decisions
       |
       v
 Hybrid search (vector similarity + FTS5 keyword)
       |
       v
 Reciprocal Rank Fusion scoring
       |
       v
 Top-K ranked results with citations
```

## Instructions

1. Take the user's query argument and call `memory_search` with it.

2. Present results in this format:

```markdown
## Search Results for "[query]"

**[N] results found**

### Result 1 (Score: 0.XX)
- **Session:** [session_id], Turn [turn_id]
- **Role:** [user/assistant]
- **Content:** [relevant excerpt]
- **Topics:** [topic1, topic2]
- **Decisions:** [any decisions from this turn]

---
### Result 2 ...
```

3. If no results are found:
```
No results found for "[query]". Try broader search terms or check /memory:status to verify the archive has data.
```

## Parameters

- **query** (required): The search text. Can be natural language, entity names, or decision keywords.
- **top_k** (optional, default 5): Number of results to return.
- **session_id** (optional): Filter results to a specific session.

## Examples

```
/memory:search database choice
/memory:search what auth method did we pick
/memory:search Redis caching
```
