---
description: View memory archive size, session count, and system health
---

# /memory:status

> See [CONNECTORS.md](../CONNECTORS.md) for required setup.

Display the current state of the memory archive including storage size, session count, and system health metrics.

## Instructions

1. Call `memory_status` with no arguments.

2. Present results:

```markdown
## Memory System Status

| Metric | Value |
|--------|-------|
| Total chunks | [N] |
| Total sessions | [N] |
| Database size | [X] MB |
| Vector search | [available/unavailable] |
| Keyword search | [available/unavailable] |

### Recent Sessions
| Session ID | Chunks | Last Updated |
|------------|--------|--------------|
| [id] | [N] | [timestamp] |
```

3. If the archive is empty:
```
Memory archive is empty. Use /memory:archive to capture your first session,
or the epistemic monitor will begin archiving automatically as you work.
```

## When to Use

- To check if the memory system is working
- To see how much history is stored
- To verify sessions were archived correctly
- When troubleshooting search results
