#!/usr/bin/env bash
# Launch the dual-agent-memory MCP server without pip install.
# Adds the src/ directory to PYTHONPATH so imports resolve.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/../src" && pwd)"

export PYTHONPATH="${SRC_DIR}${PYTHONPATH:+:$PYTHONPATH}"

DB_PATH="${1:-${HOME}/.dual_agent_memory/memory.db}"
mkdir -p "$(dirname "$DB_PATH")"

exec python3 "$SRC_DIR/dual_agent_memory/server.py" --db-path "$DB_PATH"
