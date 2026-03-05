# Dual-Agent Memory Plugin for Claude Cowork

Epistemic monitor that prevents context loss in long-running sessions. Detects contradictions, scores confidence against a persistent archive, and interrupts before the agent acts on incomplete information.

## What It Does

Long-running AI sessions fail silently when context compaction destroys critical history. The agent proceeds confidently on incomplete information, contradicts its own prior decisions, and has no signal that anything is wrong.

This plugin adds a **Memory Agent** that monitors outputs against a persistent archive, detects contradictions in real-time, and interrupts before damage is done.

## Setup

No pip install required. The plugin launches the MCP server directly from the source tree.

Add this plugin directory to Claude Cowork. The `.mcp.json` configures the local MCP server automatically via `start-server.sh`, which sets `PYTHONPATH` to the `src/` directory.

## Commands

| Command | Description |
|---------|-------------|
| `/memory:briefing` | Get a session briefing from prior conversations |
| `/memory:search <query>` | Search the archive for past decisions and context |
| `/memory:archive` | Capture current conversation to the persistent store |
| `/memory:status` | View archive size and system health |

## Skills

### Epistemic Monitoring (automatic)

Always-on contradiction detection and confidence scoring. Every assistant output is checked against the archive. Contradictions trigger hard interrupts with specific citations.

### Session Memory (automatic)

Persistent cross-session memory with automatic archiving. Session briefings at startup, hybrid search (vector + keyword), decision tracking, and zero-loss compaction capture.

## How It Works

```
User message --> Assistant reply --> memory_observe
                                         |
                                   Contradiction check
                                         |
                                   Confidence score
                                         |
                         Score < 0.5? --> Hard interrupt
                         Score < 0.7? --> Soft nudge
                         Contradiction? --> Hard interrupt (always)
```

All data stored locally in SQLite. No cloud services required.
