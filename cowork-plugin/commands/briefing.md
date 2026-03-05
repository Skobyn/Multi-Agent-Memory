---
description: Get a session briefing from prior conversations
argument-hint: "[session-id]"
---

# /memory:briefing

> See [CONNECTORS.md](../CONNECTORS.md) for required setup.

Retrieve a structured briefing of prior decisions, active topics, and entity state from the memory archive. Use this at the start of any session to restore context.

## How It Works

```
User: /memory:briefing
       |
       v
 Query archive for all sessions
       |
       v
 Aggregate: decisions, entities, topics
       |
       v
 Return structured briefing
```

## Instructions

1. Call `memory_briefing` with the provided session ID argument (or the current session ID if none given).

2. Present the briefing to the user in this format:

```markdown
## Session Briefing

### Prior Decisions
- [Decision 1] (Session X, Turn Y)
- [Decision 2] (Session X, Turn Y)

### Active Topics
- [Topic 1]
- [Topic 2]

### Key Entities
- [Entity]: [last known state]
```

3. If no prior sessions exist, inform the user:
```
No prior sessions found in the archive. This appears to be a fresh start.
Use /memory:archive to begin building your memory archive.
```

## When to Use

- At the start of every new session
- When resuming work after a break
- When the user asks "what were we working on?" or "what did we decide?"
- After context compaction to restore lost decisions
