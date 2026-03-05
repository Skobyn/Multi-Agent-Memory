---
description: Archive the current conversation to the memory store
argument-hint: "[session-id]"
---

# /memory:archive

> See [CONNECTORS.md](../CONNECTORS.md) for required setup.

Capture the current conversation history to the persistent archive. This preserves all messages, decisions, and context before they are lost to context compaction.

## How It Works

```
User: /memory:archive
       |
       v
 Collect all conversation messages
       |
       v
 Chunk into turn-level records
       |
       v
 Extract topics, entities, decisions
       |
       v
 Embed and store in SQLite archive
       |
       v
 Return stats (messages captured, chunks created)
```

## Instructions

1. Gather the full conversation history (all user and assistant messages in the current session).

2. Call `memory_archive` with:
   - `messages`: Array of `{role, content}` objects for all turns
   - `session_id`: The provided argument or auto-generated session ID

3. Report results:
```markdown
## Archive Complete

- **Messages captured:** [N]
- **Chunks created:** [N]
- **Session ID:** [session_id]
- **Elapsed:** [X.X]s

Your conversation is now preserved. Decisions and context from this session
will appear in future `/memory:briefing` and `/memory:search` results.
```

## When to Use

- Before ending a long session
- When you sense the context window is getting full
- Before starting a major topic change
- Periodically during multi-hour sessions (every ~50 turns)
- When the user says "save this" or "remember this conversation"

## Notes

- Archiving is idempotent -- re-archiving the same session updates existing records
- Zero message loss is verified by comparing input count to stored chunk count
- Each turn is individually searchable after archiving
