# Dual-Agent Memory Architecture
### Epistemic Monitor + Pull-Based Retrieval System

**Version:** 0.1 DRAFT | **Date:** March 2026 | **Author:** Scott / Apex Insights

---

## 1. Problem Definition

Long-running AI agent sessions are fundamentally constrained by a fixed context window. When that window is exhausted, standard compaction replaces conversation history with a lossy summary. Three distinct failure modes result:

| Failure Mode | Description |
|---|---|
| **Silent Ignorance** | The agent proceeds on incomplete information without knowing it has gaps. The compacted summary looks confident — the agent has no signal that critical detail was lost. |
| **Contradiction Drift** | Decisions made in session 2 conflict with decisions made in session 5. Without full history, neither the agent nor the user can detect the divergence until damage is done. |
| **Retrieval Blindness** | Pull-based memory only works if the agent knows to pull. Post-compaction, the agent lacks the metacognitive signal to recognize when it needs to query the archive. |

> **Core Insight:** The problem is not that context is lost — it's that the main agent doesn't know what it has lost. A second observer with full context can provide the metacognitive layer the main agent cannot provide for itself.

---

## 2. Proposed Solution

A dual-agent architecture that separates task execution from epistemic monitoring, combined with a pull-based semantic memory archive.

### 2.1 System Components

#### Component A — Main Agent (Task Executor)
- Standard context window, task-focused
- Executes the primary work stream
- Can invoke `memory_search` tool on-demand
- Receives nudge interrupts from the Memory Agent when coherence drops
- Does NOT proactively manage its own memory — that is delegated

#### Component B — Memory Agent (Epistemic Monitor)
- Larger context window — observes the full conversation without compacting
- Runs three monitoring functions in parallel:
  - **Contradiction detection** — compares current outputs against archived decisions
  - **Gap detection** — flags when the main agent references entities not in its current context
  - **Session-boundary briefing** — at conversation start, primes main agent with targeted archive context
- Produces a streaming confidence signal as a lightweight scalar (0.0–1.0) per main agent output
- Issues structured interrupts — not vague warnings, but specific citations with archive references

#### Component C — Memory Archive
- SQLite + sqlite-vec for hybrid vector + keyword search
- Chunks stored by conversation turn with metadata: topics, entities, timestamps, session IDs
- `before_compaction` hook captures full history before it is destroyed
- Optional consolidation pass merges superseded decisions — most recent wins with audit trail
- Exposed as `memory_search` tool callable by both agents

---

## 3. Architecture

| Layer | Behavior |
|---|---|
| **Normal Flow** | User message → Main Agent reply → User message → ... Main Agent operates at full speed, no overhead. |
| **before_compaction Hook** | Full history captured. Chunked by turn. Metadata extracted (topics, entities, dates). Embedded and stored to SQLite + sqlite-vec. Optional consolidation pass runs. |
| **Compaction** | Standard compaction proceeds. Net state summary replaces history. Nothing is permanently lost — it is in the archive. |
| **Memory Agent (continuous)** | Observes full thread on larger context. Streams confidence score per main agent output. Issues interrupt when score drops below threshold or contradiction detected. |
| **Main Agent: Pull Retrieval** | On interrupt or self-recognized gap, invokes `memory_search`. Hybrid search returns ranked chunks with session citations. Agent incorporates retrieved context and continues. |
| **Session Boundary** | Memory Agent issues targeted briefing to main agent: relevant prior decisions, open threads, entity state from archive. |

---

## 4. Confidence Score Specification

The streaming confidence score is not a self-reported certainty metric. It is computed by the Memory Agent against the archive — making it calibrated to external ground truth rather than internal model state.

| Signal | Trigger Condition |
|---|---|
| **Score < 0.7** | Memory Agent issues a soft nudge: *"Possible gap detected — consider querying archive before proceeding."* |
| **Score < 0.5** | Memory Agent issues a hard interrupt with specific citation: *"Output contradicts decision from Session 4, Turn 12. Query archive: [topic]."* |
| **Contradiction flag** | Regardless of score, any detected logical contradiction triggers an interrupt with both the current output and the archived conflicting decision surfaced side-by-side. |
| **Gap flag** | Named entity or decision reference detected in main agent output that does not appear in current context or summary — auto-prefetch triggered before output is finalized. |

> **Design Principle:** The confidence score is a trigger mechanism, not a dashboard metric. It exists to fire interventions at the right moment — not to be displayed to the user or logged as a performance indicator.

---

## 5. Definition of Done

### 5.1 MVP (P0 — Core System)

All of the following must pass before the system is considered minimally viable:

| Requirement | Priority | Acceptance Criterion |
|---|---|---|
| `before_compaction` hook operational | P0 | Full message history captured and written to archive before every compaction event. Zero message loss verified by record count. |
| SQLite + sqlite-vec store functional | P0 | Chunks stored with turn ID, session ID, timestamp, topic tags. Read/write confirmed with >100 turns of test data. |
| Hybrid search returns relevant results | P0 | Vector + keyword retrieval returns top-5 chunks with >0.75 relevance for 10 test queries drawn from archived sessions. |
| `memory_search` tool callable by main agent | P0 | Main agent can invoke tool mid-session and receive formatted results with session citations within 2 seconds. |
| Memory Agent monitors live session | P0 | Memory Agent receives full turn stream and produces confidence score per main agent output without blocking main agent response. |
| Hard interrupt fires on contradiction | P0 | Given a seeded contradiction in test data, Memory Agent detects and interrupts within 1 turn of the conflicting output being generated. |
| Session-boundary briefing delivered | P0 | At session start, Memory Agent queries archive and delivers targeted context summary to main agent before first user message is processed. |

### 5.2 Full Release (P1 — Production Ready)

| Requirement | Priority | Acceptance Criterion |
|---|---|---|
| Consolidation pass on archive | P1 | Superseded decisions flagged and merged. Most recent decision wins. Full audit trail preserved. No silent overwrites. |
| Confidence score calibration validated | P1 | Score <0.5 correlates with actual errors or gaps in >80% of test cases. False positive rate <15%. |
| Memory Agent self-compaction handled | P1 | Memory Agent context managed without loss. Strategy defined (rolling summary, tiered archive, or dedicated meta-memory layer). |
| Latency within acceptable bounds | P1 | `before_compaction` hook completes in <5 seconds for sessions up to 200 turns. Does not cause timeout in Claude Code environment. |
| Cost accounting documented | P1 | Token cost of dual-agent operation quantified per session-hour. Acceptable threshold defined for target use cases. |
| Interrupt specificity validated | P1 | 100% of hard interrupts include specific archive citation (session ID, turn, topic). Zero vague warnings in production. |

### 5.3 Out of Scope (v0.1)

- Multi-user shared memory (single-agent sessions only)
- GUI or dashboard for confidence score visualization
- Fine-tuning either agent on domain-specific memory behavior
- Real-time sync across simultaneous sessions
- Memory Agent deployed as separate service (co-process only for v0.1)

---

## 6. Open Questions

| Question | Impact |
|---|---|
| What is the correct chunking strategy for multi-turn reasoning chains? | **HIGH** — sliding window loses relational context. May require semantic chunking with explicit decision-node tagging. |
| How does Memory Agent handle its own context ceiling in very long projects? | **HIGH** — recursive compaction problem. Options: rolling summary, tiered archive, dedicated meta-memory agent. |
| What model should run the Memory Agent? | **MEDIUM** — larger context window needed but cost multiplier applies. Flash/Haiku tier for monitoring, Sonnet for interventions? |
| How is the confidence score threshold tuned per use case? | **MEDIUM** — a 0.5 threshold for a coding agent may differ from a 0.5 threshold for a research agent. Needs calibration protocol. |
| How are circular contradictions resolved? | **LOW (v0.1)** — A supersedes B supersedes A. Consolidation pass needs explicit conflict resolution policy beyond recency-wins. |

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| Cost 2–3x multiplier vs. single agent | Scope Memory Agent to high-value, long-running sessions only. Build cost threshold config. Measure before defaulting on. |
| False positives erode trust in interrupt system | Hard interrupts must be specific and cited. Tune confidence threshold empirically. Build feedback loop for false positive reporting. |
| Chunking loses reasoning chains | Evaluate semantic chunking vs. turn-based. Prototype both on real session data before committing to storage schema. |
| Memory Agent becomes a bottleneck | Memory Agent must be async and non-blocking. Main agent never waits on Memory Agent output — interrupts are advisory, not blocking gates. |

---

> **Status:** This specification is a working draft. Open questions in Section 6 must be resolved before development begins on P1 requirements. P0 requirements are unblocked.
