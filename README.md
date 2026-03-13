# Dual-Agent Memory Architecture

**Epistemic Monitor + Pull-Based Retrieval for Long-Running AI Agent Sessions**

Long-running AI agent sessions fail silently when context compaction destroys critical history. The agent proceeds confidently on incomplete information, contradicts its own prior decisions, and lacks the metacognitive signal to know it should query for what it has lost.

This system solves that with a dual-agent architecture: a **Memory Agent** that monitors the main agent's outputs against a persistent archive, detects contradictions and knowledge gaps in real-time, and interrupts before damage is done.

## The Problem

| Failure Mode | What Happens |
|---|---|
| **Silent Ignorance** | Post-compaction, the agent has no signal that critical detail was lost. The summary looks confident. |
| **Contradiction Drift** | Decisions in session 2 conflict with session 5. Neither agent nor user detects divergence until it's too late. |
| **Retrieval Blindness** | Pull-based memory only works if the agent knows to pull. After compaction, the metacognitive trigger is gone. |

**Core insight:** The problem isn't that context is lost -- it's that the main agent doesn't know what it has lost. A second observer with full context provides the metacognitive layer the main agent cannot provide for itself.

## How It Works

```
User message --> Main Agent reply --> User message --> ...
                      |
                      v
              Memory Agent (observer)
                      |
              Confidence score (0.0-1.0)
                      |
              Score < 0.7? --> Soft nudge
              Score < 0.5? --> Hard interrupt with citation
              Contradiction? --> Hard interrupt (always)
                      |
                      v
              "Output contradicts decision from Session 4, Turn 12.
               Query archive: [topic]"
```

### Architecture

```
+------------------+     +-----------------+     +------------------+
|   Main Agent     |     |  Memory Agent   |     |  Memory Archive  |
|  (task executor) |     |   (epistemic    |     | (SQLite + FTS5 + |
|                  |<----|    monitor)      |---->|   sqlite-vec)    |
|  - Does work     |     |                 |     |                  |
|  - Calls         |     | - Observes all  |     | - Chunked turns  |
|    memory_search |     |   turns         |     | - Vector search  |
|  - Receives      |     | - Scores output |     | - Keyword search |
|    interrupts    |     | - Detects       |     | - Decision index |
|                  |     |   contradictions|     | - Session metadata|
+------------------+     | - Issues        |     +------------------+
                          |   interrupts    |
                          | - Session       |
                          |   briefings     |
                          +-----------------+
```

### Key Mechanisms

**before_compaction hook** -- Captures full conversation history to the archive before context compaction destroys it. Zero message loss, verified by record count.

**Hybrid search** -- Vector similarity (sqlite-vec) + keyword matching (FTS5), fused with Reciprocal Rank Fusion. Relevant context retrieved in <200ms.

**Confidence scoring** -- Not self-reported certainty. Computed against the archive as external ground truth:
- Entity gap detection (referenced but not in current context)
- Topic drift detection (archived history exists but absent from context)
- Contradiction detection (antonym pairs, negation asymmetry, conflicting choices)

**Structured interrupts** -- Never vague. Every hard interrupt includes the specific session ID, turn number, and both the current and archived conflicting statements.

**Session briefings** -- At conversation start, the Memory Agent queries the archive and delivers prior decisions, active topics, and entity state to the main agent.

---

## Integration

Four ways to use this system depending on your platform:

### Option 1: Claude Code (MCP Server)

The MCP server exposes 6 memory tools over stdin/stdout. Claude Code connects to it and the agent calls tools directly.

**Setup:**

```bash
# Register the MCP server (run from project root)
claude mcp add dual-agent-memory -- \
  bash -c 'PYTHONPATH="$(pwd)/src" python3 src/dual_agent_memory/server.py --db-path ~/.dual_agent_memory/memory.db'
```

That's it. The `PYTHONPATH` flag tells Python where to find the source -- no installation step.

**Then** paste the contents of `scripts/CLAUDE_INSTRUCTIONS.md` into your project's `CLAUDE.md` so the agent knows when and how to use the tools:

```markdown
## Memory System Integration

At the START of every session:
1. Call `memory_briefing` with the current session ID to get prior context
2. Review the briefing for relevant prior decisions before proceeding

Before ANY context compaction:
1. Call `memory_archive` with the full conversation history

When making DECISIONS (choosing a technology, design pattern, approach):
1. Call `memory_search` with the topic to check for prior decisions
2. If a prior decision exists, either follow it or explicitly acknowledge the change

### Responding to Interrupts

If `memory_observe` or `memory_evaluate` returns an interrupt:
- **hard_interrupt**: STOP. Read the contradiction details. Query the archive. Reconcile before proceeding.
- **soft_nudge**: Consider querying the archive for additional context.
```

**Available MCP tools:**

| Tool | Purpose |
|---|---|
| `memory_search` | Search past conversations and decisions |
| `memory_observe` | Record turns + get real-time contradiction detection |
| `memory_evaluate` | Check output confidence before finalizing |
| `memory_briefing` | Get session-start context from prior sessions |
| `memory_archive` | Preserve full history before compaction |
| `memory_status` | View archive size, costs, system health |

---

### Option 2: OpenClaw Plugin

A native TypeScript OpenClaw plugin lives in `openclaw-plugin/`. It registers a **Context Engine** (handles archival and context assembly), **Agent Tools** (search, evaluate, briefing, archive, status), an **Epistemic Monitor** lifecycle hook, a **CLI command**, and a **background consolidation service** — all in one plugin.

**Prerequisites:**

- Node.js 22+
- OpenClaw installed (`npm i -g openclaw`)
- `better-sqlite3` (peer dependency — installed automatically by OpenClaw)

**Install:**

```bash
# From npm
npm install openclaw-dual-agent-memory

# Or install as an OpenClaw plugin
openclaw plugins install openclaw-dual-agent-memory

# Local development (symlink — changes take effect on restart)
openclaw plugins install -l ./openclaw-plugin
```

**Configure** in your `openclaw.config.json`:

```json
{
  "plugins": {
    "slots": {
      "contextEngine": "dual-agent-memory"
    },
    "entries": {
      "dual-agent-memory": {
        "enabled": true,
        "config": {
          "dbPath": "~/.dual_agent_memory/memory.db",
          "softNudgeThreshold": 0.7,
          "hardInterruptThreshold": 0.5,
          "searchTopK": 5,
          "vectorWeight": 0.7,
          "keywordWeight": 0.3,
          "autoEvaluate": true,
          "autoArchiveOnCompact": true
        }
      }
    }
  }
}
```

All config fields are optional — defaults match the values shown above.

**What the plugin does automatically:**

- **Context Engine** — archives the full conversation before compaction (zero message loss) and injects relevant archived context into prompts during assembly.
- **Epistemic Monitor** (`before_prompt_build` hook) — evaluates every assistant turn against the archive. When contradictions, gaps, or topic drift are detected, warnings are injected into the system context so the agent self-corrects.
- **Background Consolidation** — a service runs every 5 minutes to mark older superseded decisions, keeping the archive clean while preserving the audit trail.

**Agent tools registered:**

| Tool | Purpose |
|---|---|
| `memory_search` | Hybrid vector + keyword search over archived conversations |
| `memory_evaluate` | Score an output for confidence (0-1), detect contradictions and gaps |
| `memory_briefing` | Session-start briefing with prior decisions, topics, entities |
| `memory_archive` | Manually archive messages to the store |
| `memory_status` | View archive size, config, observed turns |

**CLI commands:**

```bash
openclaw memory status       # Archive size, session info, DB path
openclaw memory search "authentication decisions"   # Search with hybrid retrieval
openclaw memory decisions    # List all archived decisions chronologically
```

**Plugin structure:**

```
openclaw-plugin/
  openclaw.plugin.json    # Plugin manifest with config schema + UI hints
  package.json            # npm metadata with openclaw.extensions
  tsconfig.json           # TypeScript config
  src/
    index.ts              # All registrations (context engine, tools, hook, CLI, service)
  tests/
    index.test.ts         # Vitest tests covering registration and tool behavior
```

**Run plugin tests:**

```bash
cd openclaw-plugin
npm install
npx vitest run
```

---

### Option 3: Claude Cowork (Plugin)

A ready-made Cowork plugin lives in `cowork-plugin/`. It provides slash commands, always-on skills, and connects to the same MCP server.

**Setup:**

Copy the `cowork-plugin/` directory to your Cowork plugins location. The plugin runs the MCP server directly from the source tree via a launcher script -- no installation needed.

```
cowork-plugin/
  .claude-plugin/plugin.json   # Plugin manifest
  .mcp.json                    # MCP server config (uses start-server.sh)
  start-server.sh              # Launcher: sets PYTHONPATH, runs server.py
  CONNECTORS.md                # Documents available tools
  commands/
    briefing.md                # /memory:briefing -- session-start context
    search.md                  # /memory:search <query> -- archive search
    archive.md                 # /memory:archive -- save conversation
    status.md                  # /memory:status -- system health
  skills/
    epistemic-monitoring/      # Always-on contradiction detection
      SKILL.md
    session-memory/            # Cross-session persistence
      SKILL.md
```

**What the plugin does automatically:**

- **Epistemic monitoring** -- every assistant output is checked against the archive for contradictions. Hard interrupts fire with citations when a conflict is detected.
- **Session memory** -- briefings at session start, decision tracking, hybrid search across all past sessions.

**Slash commands:**

| Command | What it does |
|---|---|
| `/memory:briefing` | Get prior decisions and context at session start |
| `/memory:search <query>` | Search the archive with hybrid vector + keyword |
| `/memory:archive` | Capture current conversation before it's lost |
| `/memory:status` | Check archive size and system health |

---

### Option 4: Python Agent Frameworks (OpenClaw Python, LangChain, CrewAI, etc.)

Works with OpenClaw, LangChain, CrewAI, custom loops, or any Python-based agent. Import directly from the source tree.

**Setup:**

Add the `src/` directory to your Python path. No pip install needed.

```python
import sys
sys.path.insert(0, "/path/to/dual-agent-memory/src")

from dual_agent_memory.integration import DualAgentMemory
```

Or set the environment variable:

```bash
export PYTHONPATH="/path/to/dual-agent-memory/src:$PYTHONPATH"
python your_agent.py
```

**Basic usage:**

```python
from dual_agent_memory.integration import DualAgentMemory

with DualAgentMemory("./memory.db", session_id="session-1") as memory:
    # Session start -- get briefing from prior sessions
    briefing = memory.start_session()
    print(briefing["briefing"])

    # Your agent loop
    while True:
        user_input = get_user_input()
        memory.on_user_message(user_input)

        assistant_output = your_agent.run(user_input)
        result = memory.on_assistant_message(assistant_output)

        # Check for contradictions
        if result["interrupt"]:
            if result["interrupt"]["severity"] == "hard_interrupt":
                # Agent contradicted a prior decision
                print(result["interrupt"]["message"])
                context = memory.search(result["interrupt"]["suggested_query"])
                assistant_output = your_agent.run(
                    f"CORRECTION NEEDED: {result['interrupt']['message']}\n"
                    f"Archive context: {context}"
                )

    # Session end -- archive everything
    memory.end_session(all_messages)
```

**Tool-use integration** (for agents that support tool calling):

```python
from dual_agent_memory.integration import DualAgentMemory

memory = DualAgentMemory("./memory.db")

# Get tool definitions in Claude/OpenAI format
tools = memory.get_tool_definitions()
# Pass `tools` to your LLM's tool parameter

# When the LLM calls a tool:
result = memory.handle_tool_call(tool_name, arguments)
```

**Low-level API** (for custom pipelines):

```python
import sys
sys.path.insert(0, "/path/to/dual-agent-memory/src")

from dual_agent_memory.archive import MemoryArchive
from dual_agent_memory.embedder import Embedder
from dual_agent_memory.monitor import MemoryAgent
from dual_agent_memory.config import Config

config = Config(db_path="./memory.db")
archive = MemoryArchive(config)
embedder = Embedder(config)
agent = MemoryAgent(archive, embedder, config)

# Session briefing
briefing = agent.generate_briefing("session-42")

# Process turns
signal, interrupt = agent.process_turn("user", "Let's switch to MongoDB.")
signal, interrupt = agent.process_turn("assistant", "We decided to use MongoDB.")

if interrupt:
    print(f"[{interrupt.severity}] {interrupt.message}")
    # [hard_interrupt] CONTRADICTION: Output conflicts with archived decisions.
    #       Current: "decided to use MongoDB"
    #       vs Archived: "chose PostgreSQL" (Session session-1, Turn 4)
```

---

## Running Tests

```bash
PYTHONPATH=src pytest tests/ -v
```

**50 tests** across 3 files covering all P0 and P1 acceptance criteria:

- `test_archive.py` -- Storage, chunking, embedding, hybrid search (19 tests)
- `test_monitor.py` -- Contradiction detection, confidence scoring, interrupts, hooks, tools (18 tests)
- `test_p1_requirements.py` -- Consolidation, calibration, latency, interrupt specificity, self-compaction (13 tests)

### Validated Performance

- before_compaction: <5 seconds for 200 turns
- Search latency: <200ms average per query
- Confidence calibration: >80% true positive rate, <15% false positive rate
- Zero message loss on compaction (verified by record count)

## Components

| Module | Class | Purpose |
|---|---|---|
| `archive.py` | `MemoryArchive` | SQLite + FTS5 + sqlite-vec storage. Graceful fallback when sqlite-vec unavailable. |
| `chunker.py` | `Chunker` | Turn-level chunking with rule-based extraction of topics, entities, and decisions. |
| `embedder.py` | `Embedder` | Hash-based deterministic embeddings (pluggable -- swap in sentence-transformers). |
| `search.py` | `HybridSearch` | Vector + keyword search fused with Reciprocal Rank Fusion (k=60). |
| `contradiction.py` | `ContradictionDetector` | Antonym pairs, negation asymmetry, conflicting choice detection. |
| `confidence.py` | `ConfidenceScorer` | Per-output scoring against archive: gaps, drift, contradictions. |
| `monitor.py` | `MemoryAgent` | The epistemic monitor. Observes, evaluates, interrupts, briefs. |
| `hooks.py` | `CompactionHook` | before_compaction capture + decision consolidation with audit trail. |
| `tools.py` | `MemorySearchTool` | Claude tool-use interface for archive retrieval. |
| `cost.py` | `CostTracker` | Token accounting with per-model USD estimates and use-case thresholds. |
| `server.py` | `MemoryMCPServer` | MCP server (JSON-RPC 2.0 over stdio) exposing 6 tools. |
| `integration.py` | `DualAgentMemory` | High-level API for any Python agent framework. |
| `openclaw-plugin/src/index.ts` | `dualAgentMemoryPlugin` | OpenClaw plugin: context engine + tools + epistemic hook + CLI + consolidation service. |

## Confidence Score Thresholds

| Score | Action |
|---|---|
| > 0.7 | No interrupt. Output is well-grounded. |
| 0.5 - 0.7 | **Soft nudge**: "Possible gap detected -- consider querying archive." |
| < 0.5 | **Hard interrupt**: Specific citation with session ID, turn, and conflicting text. |
| Contradiction | **Hard interrupt** regardless of score. Both statements surfaced side-by-side. |

## Self-Compaction

The Memory Agent manages its own context ceiling via rolling compaction:

1. When observed turns exceed `max_observed_turns` (default 50), the oldest half is compacted
2. A structured summary (decisions, entities, topics) is accumulated in a rolling summary
3. Compacted turns are archived to the same store -- nothing is lost, just searchable
4. The context summary prepends the rolling history before recent turns

## Pluggable Embeddings

The default embedder uses deterministic hash-based vectors (no ML model required). To use real semantic embeddings:

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2")

embedder = Embedder(
    config=Config(embedding_dim=384),
    embed_fn=lambda text: model.encode(text).tolist()
)
```

## Cost Tracking

```python
summary = agent.cost_tracker.get_summary()
print(f"Tokens/hour: {summary['tokens_per_hour']:.0f}")
print(f"Estimated cost (Haiku): ${summary['estimated_cost_per_hour_usd']['haiku']:.4f}/hr")
print(f"Monitor overhead: {summary['overhead_vs_single_agent']['monitor_overhead_pct']:.1f}%")
```

## Design Decisions

**Why a second agent instead of self-monitoring?** A model cannot reliably detect its own knowledge gaps post-compaction. The compacted summary looks complete to the model -- it has no signal that information was lost. An external observer with access to the full archive can provide calibrated confidence.

**Why rule-based contradiction detection?** For the MVP, rule-based detection (antonym pairs, negation patterns, conflicting choices) provides high specificity with zero latency. False positives erode trust faster than false negatives. The architecture supports swapping in LLM-based detection for P2.

**Why SQLite?** Single-file deployment, zero configuration, FTS5 built-in, sqlite-vec available as extension. No database server required. Suitable for single-agent sessions (multi-user is out of scope for v0.1).

**Why Reciprocal Rank Fusion?** RRF combines vector and keyword rankings without requiring score calibration between the two systems. It's robust to score distribution differences and well-studied in information retrieval literature.

## Out of Scope (v0.1)

- Multi-user shared memory
- GUI or dashboard for confidence visualization
- Fine-tuning agents on domain-specific memory behavior
- Real-time sync across simultaneous sessions
- Memory Agent as a separate service (co-process only)

## License

MIT
