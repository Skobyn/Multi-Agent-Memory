# Connectors

This plugin uses one local MCP server:

| Server | Type | Purpose |
|--------|------|---------|
| **dual-agent-memory** | stdio (local) | SQLite-backed memory archive with hybrid search, contradiction detection, and confidence scoring |

## Required Setup

No installation required. The plugin launches the MCP server directly from the source tree via `start-server.sh`, which sets `PYTHONPATH` automatically.

The server runs locally via stdio transport. No external API keys or cloud services are required. All data is stored in a local SQLite database at `~/.dual_agent_memory/memory.db`.

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `memory_search` | Search past conversations and archived decisions |
| `memory_observe` | Record conversation turns and get real-time contradiction detection |
| `memory_evaluate` | Check output confidence before finalizing a response |
| `memory_briefing` | Get a structured briefing from prior sessions at session start |
| `memory_archive` | Preserve full conversation history before context compaction |
| `memory_status` | View archive size, session count, and system health |
