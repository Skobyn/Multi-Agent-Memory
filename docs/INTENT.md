# Intent: Epistemic Monitor v2

**Status:** Approved intent for the next build session. Companion: [`DESIGN.md`](DESIGN.md).
**Date:** 2026-10-27. **Owner:** Scott / Apex Insights.
**Platforms in scope:** Claude Code CLI first, Claude Agent SDK (TypeScript) second. OpenClaw is out of scope.

## 1. Thesis

Long-running agent sessions do not fail because context is lost. They fail because the agent
does not know what it lost, and nothing checks its next action against what it previously
committed to. Memory products (Anthropic auto-memory, Letta, Mem0, Supermemory, Managed
Agents "dreaming") all *remember*. None of them *hold the line*: none extract typed decisions
and constraints, none verify that a compaction summary preserved them, and none test a live
action against them before it executes.

v2 is **the enforcement layer for agent memory**. It is a Claude Code plugin built on hooks,
so it runs whether or not the main agent cooperates, and it can deny, rewrite, or redirect,
not merely advise.

## 2. What v0.1 got wrong (so we do not repeat it)

| v0.1 | Consequence | v2 rule |
|---|---|---|
| "Memory Agent" was regex (antonym table, capitalized-word entities, Jaccard overlap) and a SHA-256 hash embedder | 0.0% contradiction detection in all 30 benchmark runs; misses "Going with MongoDB" | A model judges, deterministic code enforces. Real embeddings. |
| Monitor ran only when the main agent voluntarily called `memory_observe` | The exact "retrieval blindness" the spec warned about | Hooks-first. Zero cooperation required from the main agent. |
| Interrupts were strings returned from a tool call | Nothing could ever block an action | `PreToolUse` deny/rewrite, `Stop` block, `PostToolBatch` context. |
| Benchmark measured lexical recall of its own seed strings | 100% "recall", meaningless | Measure violations of prior decisions after compaction, with baselines. |
| Confidence score was an additive heuristic | Uncalibrated, never validated | Calibrated thresholds from an intervention log; cheap gate, expensive advisor. |

## 3. Why now

- **The premise is now published and replicated.** LLM compaction summaries keep ≤16–17% of
  injected constraints (*Lost in Compaction* 2608.11242, *Compact-Bench* 2610.31710).
  Constraint violations go 0% → ~30% after one compaction and ~78% after four; re-injecting
  constraints verbatim restores 0% (*Governance Decay* 2606.22528). Agents that retrieve an
  updated fact still act on the stale one (*STALE* 2605.06527). Retrieval is not the
  bottleneck. Action-time checking is.
- **The hook surfaces now exist** (verified in Claude Code 2.1.311): `PreCompact` with the
  transcript path, `PostCompact` with the summary text, `SessionStart` matcher `compact`,
  `PreToolUse` deny / `updatedInput`, `PostToolBatch`, `Stop` block-with-reason, and built-in
  `prompt` / `agent` hook types for model-judged checks.
- **Cheap typed decision models exist.** TypeSafe's Jev (System One, Sept 2026) answers a typed
  yes/no or choice with a probability in ~100–400 ms at ~$1e-4. Accuracy trails LLMs, so it is
  the gate in front of an LLM advisor, not the judge (the REFLEX pattern, 2609.26532).
- **Steering with a cheap monitor is proven to pay.** LivePlan (2608.06701): rule-based
  pre-execution monitor + LLM advisor on flag, +9.9% SWE-bench Verified/Pro at ~$0.08/task.

## 4. What we are building

Three capabilities, in priority order. Each is independently valuable and independently measurable.

1. **Decision Ledger.** Typed entries (decision, constraint, premise, commitment) extracted
   mechanically from the transcript at compaction and turn boundaries, each with scope,
   provenance, supersession links, and where possible a **falsifier**: a machine-checkable
   predicate ("no file under `src/` imports `pymongo`").
2. **Compaction Audit + Pinning.** After every compaction, diff the ledger against the summary,
   re-inject exactly what was dropped, and report *constraint retention %*.
3. **Conformance Gate.** Before every file edit or shell command: falsifiers (deterministic),
   then decision-model scoring of relevant ledger entries, then an LLM advisor only on a
   flag, then a *resample request* before any deny, under a per-session intervention budget.

Secondary (later phases): decision dependency graph with cascading supersession,
provenance-weighted enforcement, shared ledger for subagents/agent teams, a live panel (mod),
and the DecisionDrift benchmark.

## 5. Principles

1. **Hooks-first.** The loop must work with no MCP tool calls from the main agent. MCP tools
   (`memory_search`, `memory_briefing`) are a convenience, not the mechanism.
2. **Mechanical extraction over summarization.** Decisions are pulled from the raw transcript
   with a structured-output extractor, never from the compaction summary.
3. **Typed and checkable.** Every entry has a type, scope, and provenance. Prefer a falsifier
   over a semantic judgment whenever one can be written.
4. **Cheap gate, expensive advisor.** Deterministic → decision model → LLM. The LLM sees the
   diff, the entries, and the rationale, never the prose alone.
5. **Resample before deny.** A conflict first asks the agent to revise or to explicitly
   supersede the decision with a reason. Hard denies are budgeted.
6. **Provenance decides force.** User-made decisions are enforced hard; agent-inferred
   decisions soft; summary-derived decisions only after re-verification.
7. **Every intervention is cited and logged.** Session, turn, entry id, tier, score, outcome.
   The log is the calibration set and the benchmark input.
8. **Measure or it did not happen.** Retention %, violation rate, false-deny rate, latency,
   cost per session-hour, against baselines.

## 6. Non-goals for v2

- OpenClaw plugin (dropped). Cowork plugin is kept only as a thin wrapper if cheap.
- Replacing Anthropic auto-memory or CLAUDE.md; v2 sits beside them and may write into the
  auto-memory directory.
- GUI beyond an optional mod panel.
- Fine-tuning any model. JEPA-style latent predictors (research track only).
- Multi-user shared ledger. Real-time sync across simultaneous sessions.

## 7. Success criteria

| Metric | Baseline (literature / v0.1) | v2 target |
|---|---|---|
| Constraint retention after compaction | ≤17% (summary alone) | ≥95% (audit + pinning) |
| Post-compaction violation rate (DecisionDrift-Bench) | ~30% after 1 compaction | ≤5% |
| False-deny rate (gate denies a consistent action) | n/a | ≤2% of tool calls |
| PreToolUse latency, unflagged path (p50 / p95) | n/a | ≤300 ms / ≤800 ms |
| Monitor cost | n/a | ≤10% of main-agent token cost per session-hour |
| Interventions with a specific citation | v0.1 spec target 100% | 100% |

Phase exit criteria are in `DESIGN.md` §10.

## 8. Risks and how the design answers them

| Risk | Answer |
|---|---|
| False denies erode trust faster than misses | Resample first, deny rarely, budget denies, cite every one |
| Hook timeouts / latency | Deterministic + retrieval tiers under 300 ms; model tiers only on flag |
| Transcript JSONL format is undocumented and shifts between versions | Capture at `PreCompact`; parse defensively; version-pin the reader with tests |
| Decision-model accuracy below LLMs | Use as a gate with a locally calibrated threshold; never as the final word |
| Agent "knows it is watched" and games the monitor | Monitor sees diffs and commands, not self-reports; no monitor text in the main prompt beyond citations |
| Stop-hook block cap (8 consecutive) | At most one Stop block per turn; prefer `PreToolUse` and `PostToolBatch` |
