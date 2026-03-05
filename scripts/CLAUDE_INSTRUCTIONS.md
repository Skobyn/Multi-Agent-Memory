## Memory System Integration

This project uses a dual-agent memory system that prevents context loss across sessions.

### Automatic Behaviors

At the START of every session:
1. Call `memory_briefing` with the current session ID to get prior context
2. Review the briefing for relevant prior decisions before proceeding

Before ANY context compaction:
1. Call `memory_archive` with the full conversation history

When making DECISIONS (choosing a technology, design pattern, approach):
1. Call `memory_search` with the topic to check for prior decisions
2. If a prior decision exists, either follow it or explicitly acknowledge the change

### Available Memory Tools

| Tool | When to Use |
|---|---|
| `memory_search` | Before making decisions. When referencing past work. When unsure about prior context. |
| `memory_observe` | After each user/assistant message to maintain monitoring (optional -- the hook handles this automatically if configured). |
| `memory_evaluate` | Before finalizing important outputs. Returns confidence score and contradiction flags. |
| `memory_briefing` | At session start. Returns prior decisions, active topics, entity state. |
| `memory_archive` | Before context compaction. At session end. Preserves full history. |
| `memory_status` | To check archive size, cost tracking, and system health. |

### Responding to Interrupts

If `memory_observe` or `memory_evaluate` returns an interrupt:
- **hard_interrupt**: STOP. Read the contradiction details. Query the archive using the `suggested_query`. Reconcile before proceeding.
- **soft_nudge**: Consider querying the archive for additional context. Proceed with caution.

### Session ID Convention

Use a consistent session ID format: `{project}-{date}-{sequence}`
Example: `myapp-20260305-1`
