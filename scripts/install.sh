#!/usr/bin/env bash
# Set up dual-agent memory system for Claude Code (no pip install required)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$PROJECT_ROOT/src"

echo "=== Dual-Agent Memory System Setup ==="

# 1. Verify source exists
echo "[1/3] Checking source tree..."
if [ ! -f "$SRC_DIR/dual_agent_memory/server.py" ]; then
    echo "  ERROR: server.py not found at $SRC_DIR/dual_agent_memory/server.py"
    exit 1
fi
echo "  Source: OK"

# 2. Verify imports work
echo "[2/3] Verifying Python imports..."
PYTHONPATH="$SRC_DIR" python3 -c "from dual_agent_memory.integration import DualAgentMemory; print('  Imports: OK')" 2>/dev/null || \
PYTHONPATH="$SRC_DIR" python -c "from dual_agent_memory.integration import DualAgentMemory; print('  Imports: OK')"

# 3. Register MCP server with Claude Code
echo "[3/3] Registering MCP server with Claude Code..."
DB_PATH="${MEMORY_DB_PATH:-$HOME/.dual_agent_memory/memory.db}"
mkdir -p "$(dirname "$DB_PATH")"

claude mcp add dual-agent-memory -- \
    bash -c "PYTHONPATH=\"$SRC_DIR\" python3 \"$SRC_DIR/dual_agent_memory/server.py\" --db-path \"$DB_PATH\"" 2>/dev/null || \
echo "  Note: Run manually:"
echo "    claude mcp add dual-agent-memory -- bash -c 'PYTHONPATH=\"$SRC_DIR\" python3 \"$SRC_DIR/dual_agent_memory/server.py\" --db-path \"$DB_PATH\"'"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "No pip install needed. The MCP server runs directly from the source tree."
echo ""
echo "The following MCP tools are now available in Claude Code:"
echo "  - memory_search     : Search past conversations"
echo "  - memory_observe    : Record conversation turns"
echo "  - memory_evaluate   : Check output confidence"
echo "  - memory_briefing   : Get session-start briefing"
echo "  - memory_archive    : Archive messages before compaction"
echo "  - memory_status     : View system status"
echo ""
echo "Add these instructions to your project's CLAUDE.md:"
echo "  See: $PROJECT_ROOT/scripts/CLAUDE_INSTRUCTIONS.md"
