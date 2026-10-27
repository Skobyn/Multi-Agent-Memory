# Design: Epistemic Monitor v2

**Status:** Build-ready design for the next session. Companion: [`INTENT.md`](INTENT.md).
**Date:** 2026-10-27. Facts about Claude Code hooks below were verified against the installed
2.1.311 binary and the official docs (https://code.claude.com/docs/en/hooks). The transcript
JSONL shape is undocumented and must be treated as unstable.

## 1. Architecture

```
                       Claude Code session (main agent)
  ┌───────────────────────────────────────────────────────────────────────────┐
  │ UserPromptSubmit   PreToolUse ──► [Conformance Gate] ──► allow/deny/rewrite │
  │                    PostToolBatch ─► additionalContext (citations)          │
  │                    Stop ─────────► [Turn Check] ──► block-with-reason (≤1) │
  │ PreCompact ───────► [Archiver + Extractor]                                 │
  │ PostCompact ──────► [Compaction Auditor]                                   │
  │ SessionStart[compact|startup|resume|fork] ◄── [Pinner / Briefing]          │
  │ SessionEnd ───────► [Archiver + Extractor]                                 │
  │ SubagentStart ────► ledger injected   SubagentStop ──► [Turn Check]        │
  └───────────────┬───────────────────────────────────────────────┬───────────┘
                  │ reads transcript_path JSONL                    │ writes
                  ▼                                                ▼
        ┌──────────────────┐   ┌────────────────────┐   ┌────────────────────┐
        │ Transcript Reader│──►│ Decision Ledger    │◄──│ Intervention Log   │
        │ (version-pinned) │   │ ledger.jsonl +     │   │ interventions.jsonl│
        └──────────────────┘   │ SQLite/FTS5/vec idx│   └────────────────────┘
                               └─────────┬──────────┘
                                         │ retrieval
                    ┌────────────────────┼──────────────────────┐
                    ▼                    ▼                      ▼
             Tier 0 Falsifiers    Tier 2 Decision Model    Tier 3 LLM Advisor
             (deterministic)      (Jev Noul, calibrated τ) (diff+entries+rationale)
```

Everything runs out-of-process as hook commands (Python, stdlib + sqlite). No MCP server is
required for the loop. An optional MCP server exposes `ledger_search`, `ledger_briefing`,
`ledger_supersede`, `ledger_status` for the agent and the user.

## 2. Storage layout

Machine-local, per project, beside Claude Code's own auto-memory so nothing is committed to
the user's repo by default:

```
~/.claude/projects/<project-slug>/epistemic/
  ledger.jsonl            # append-only event log of ledger entries and supersessions
  index.db                # SQLite: entries table, FTS5, sqlite-vec (fallback: numpy cosine)
  interventions.jsonl     # every gate/turn-check decision
  pinned.md               # last computed pin block (≤10,000 chars)
  calibration.json        # decision-model threshold τ, last fit date, sample size
  transcripts/<session>.jsonl.gz   # verbatim capture at PreCompact / SessionEnd
```

`<project-slug>` is the same slug Claude Code uses for `~/.claude/projects/` (cwd with
non-alphanumerics replaced by `-`). An optional human-readable export `docs/DECISIONS.md`
can be written into the repo on request; it is never the source of truth.

Reuse from v0.1: `archive.py` (SQLite + FTS5 + sqlite-vec with fallback) and `search.py`
(RRF fusion) are kept and adapted to the entry schema. `embedder.py` keeps its pluggable
interface but the hash default is removed; the default becomes a real embedding model
(OpenRouter-proxied API or local sentence-transformers). `chunker.py`, `contradiction.py`,
`confidence.py`, `monitor.py` are deleted. `tests/benchmark_*` are replaced (§9).

## 3. Ledger entry schema

```jsonc
{
  "id": "D-0042",                      // D- decision, C- constraint, P- premise, K- commitment
  "type": "decision",                  // decision | constraint | premise | commitment
  "statement": "Use PostgreSQL as the primary database.",
  "rationale": "Relational data, team familiarity; user chose it over MongoDB.",
  "scope": {
    "paths": ["src/db/**", "alembic/**"],      // globs; empty = global
    "tools": ["Edit", "Write", "Bash"],        // tool names the gate applies to
    "topics": ["database", "orm", "migrations"]
  },
  "falsifiers": [                              // machine-checkable; may be empty
    {"kind": "diff_regex_forbidden", "pattern": "\\bimport\\s+pymongo\\b|\\bmongoose\\b"},
    {"kind": "path_glob_forbidden", "glob": "src/db/mongo/**"},
    {"kind": "command_regex_forbidden", "pattern": "pip install (pymongo|motor)"}
  ],
  "provenance": {
    "source": "user",                  // user | agent | summary | hook
    "session_id": "…", "turn_uuid": "…", "timestamp": "2026-10-27T18:02:11Z",
    "confirmed_by_user": true,
    "extraction_confidence": 0.93
  },
  "status": "active",                  // active | superseded | orphaned | retired
  "supersedes": [], "superseded_by": null,
  "depends_on": [],                    // entry ids this one presupposes
  "valid_from": "2026-10-27T18:02:11Z", "valid_to": null,
  "last_verified": "2026-10-27T18:02:11Z",
  "embedding_ref": "idx:42"
}
```

Falsifier kinds for v2: `diff_regex_forbidden`, `diff_regex_required`, `path_glob_forbidden`,
`path_glob_required_with` (e.g. new `src/api/*.py` requires `tests/api/test_*.py`),
`command_regex_forbidden`, `shell_check` (a command that must exit 0; run only in Tier 3
because it is slow). Anything not expressible as one of these is left to Tiers 2–3.

Supersession is an event, not an edit: `{"event":"supersede","old":"D-0042","new":"D-0051",
"reason":"…","by":"user|agent","session_id":…}`. The index is rebuilt from events.

## 4. Hook wiring

`hooks.json` shipped by the plugin. All commands are `python3 -I <plugin>/hooks/<name>.py`.

| Event / matcher | Script | Reads | Writes / returns | Budget |
|---|---|---|---|---|
| `PreCompact` (`auto`, `manual`) | `archive_and_extract.py` | `transcript_path`, `custom_instructions`, `trigger` | gz capture; ledger entries since last watermark | ≤30 s (hook default 600 s) |
| `PostCompact` | `audit_compaction.py` | `compact_summary` | retention report; `pinned.md` | ≤10 s |
| `SessionStart` (`compact`) | `pin.py` | `pinned.md` | `hookSpecificOutput.additionalContext` ≤10,000 chars | ≤2 s |
| `SessionStart` (`startup`, `resume`, `fork`) | `briefing.py` | ledger | `additionalContext`: active entries grouped by type, user-provenance first, orphaned entries flagged | ≤2 s |
| `PreToolUse` (`Edit\|Write\|MultiEdit\|NotebookEdit\|Bash`) | `gate.py` | `tool_input`, ledger, calibration | `permissionDecision` allow/deny, `updatedInput`, `additionalContext`, `permissionDecisionReason` | Tier 0–1 ≤300 ms; Tier 2 ≤1 s; Tier 3 ≤20 s |
| `PostToolBatch` | `cite.py` | last gate results | `additionalContext` with citations for entries touched this batch (no decision) | ≤1 s |
| `Stop` | `turn_check.py` | `last_assistant_message`, `stop_hook_active` | `decision: "block"` + `reason` at most once per turn; else incremental extraction | ≤20 s |
| `SessionEnd` | `archive_and_extract.py` | `transcript_path` | capture + extraction | ≤1.5 s default; raise `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` or do capture only |
| `SubagentStart` | `briefing.py --compact` | ledger | `additionalContext` (≤3,000 chars) | ≤2 s |
| `SubagentStop` | `turn_check.py --subagent` | `agent_transcript_path` | `decision: "block"` + reason if the subagent's result contradicts a user-provenance entry | ≤20 s |

Notes verified from docs: exit code 2 blocks on blockable events regardless of JSON;
`PreCompact` cannot inject context (hence `SessionStart[compact]`); `PostCompact` cannot edit
the summary; `Stop` has an 8-consecutive-block cap (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`);
`additionalContext` is capped at 10,000 chars; the transcript file "may lag" and may lack the
final message at `Stop` time, so `last_assistant_message` is used there; hooks on the same event
run in parallel and the most restrictive decision wins.

## 5. Transcript reader

- Input: `~/.claude/projects/<slug>/<session>.jsonl` (append-only; subagents under
  `<session>/subagents/agent-<id>.jsonl`).
- Rows used: `type in {user, assistant}` with `message.content[]` blocks (`text`, `tool_use`,
  `tool_result`); skip `isSidechain`, `isMeta`; detect compaction via
  `type=="system" && subtype=="compact_boundary"` and the following `isCompactSummary` user row.
- Watermark: last processed `uuid` per session stored in `index.db`, so extraction is
  incremental at `Stop` and complete at `PreCompact`.
- Must be version-pinned with fixture files from at least two Claude Code versions and fail
  soft (log, skip row) on unknown shapes.

## 6. Extractor

- Model: Haiku-class via the Anthropic API (or OpenRouter, which this environment proxies),
  structured output conforming to §3 minus `id`/`status`/`embedding_ref`.
- Input window: turns since the watermark, plus the current active ledger (ids + statements)
  so the model can emit `supersedes` and `depends_on` references and avoid duplicates.
- Prompt contract (summary): extract only explicit decisions, constraints, premises, and
  commitments to the user; quote the source turn; set `provenance.source=user` only when the
  user stated or confirmed it; propose falsifiers only when they are obviously correct;
  mark `extraction_confidence`.
- Post-processing: embedding dedup (cosine ≥ 0.92 → candidate merge), LLM merge decision for
  candidates, supersession events for explicit reversals, `depends_on` closure.
- Never read the compaction summary as a source. Summary-derived entries are only created by
  the auditor (§7) with `provenance.source=summary` and `status=active` but `confirmed_by_user=false`.

## 7. Compaction auditor and pinner

1. `PostCompact` receives `compact_summary`. For each active entry: anchor check (statement
   keywords present), embedding similarity to summary sentences, and a decision-model Noul
   "Does this summary preserve decision D?" with probability.
2. Entries judged absent are written to `pinned.md` as a verbatim block, user-provenance
   first, with ids and one-line rationale, trimmed to 10,000 chars (overflow → "N more; run
   `/ledger` to list").
3. Record `retention = preserved / active` to `interventions.jsonl` with `kind=compaction_audit`.
4. `SessionStart[compact]` emits the block as `additionalContext`. Because Claude Code also
   re-injects `MEMORY.md` (first 200 lines / 25 KB) after compaction, the pinner may
   additionally maintain a `## Active decisions (epistemic monitor)` section in `MEMORY.md`
   capped at 40 lines, so the ledger survives even if hooks are disabled.

## 8. Conformance gate (`PreToolUse`)

Tiers run in order and short-circuit:

- **Tier 0, falsifiers (≤50 ms).** Compute the candidate diff (for `Edit`/`Write` from
  `tool_input`; for `Bash` the command string). Run every active entry's falsifiers whose
  scope matches the path/tool. A tripped falsifier is a confirmed conflict → go to Resample.
- **Tier 1, retrieval (≤100 ms).** Scope match (glob/tool/topic) ∪ top-k by embedding of the
  diff/command against entry statements (k=5). No candidates → `allow`.
- **Tier 2, decision model (≤1 s, parallel).** For each candidate, Noul: "Given decision
  `<statement>` (rationale `<…>`), is this change consistent with it? Change: `<diff excerpt>`".
  Probability of *inconsistent* ≥ τ → flag. τ comes from `calibration.json` (§8.1); until
  calibrated, τ = 0.75 and Tier 2 is advisory-only (logs, never denies).
- **Tier 3, LLM advisor (≤20 s, only on flag).** Sonnet-class call (or a `type: "prompt"`
  hook) with: the diff, the flagged entries with rationale and provenance, the last 3 turns.
  Output: `{verdict: consistent|conflict|supersede_proposed, entry_ids, message}`.
- **Resample before deny.** First conflict on an entry in this turn → return
  `permissionDecision: "deny"` with reason: *"This change conflicts with D-0042 (user decision,
  Session s, turn t): '<statement>'. Either revise the change, or supersede it explicitly by
  stating `SUPERSEDE D-0042: <reason>` in your reply."* The agent's next attempt is re-gated;
  an explicit `SUPERSEDE` line in `last_assistant_message` (caught at `Stop`) writes a
  supersession event with `by=agent` (or `by=user` if the user's prompt requested the change).
- **Force by provenance.** `source=user` → deny path as above. `source=agent` → `allow` with
  `additionalContext` citation and a soft warning; deny only if a falsifier tripped.
  `source=summary` → citation only.
- **Harm budget.** Max `K=5` denies per session (configurable); beyond K the gate is
  advisory-only and says so once. Every decision, tier, score, and outcome is logged.
- **Latency guard.** If Tier 2/3 exceed budget, fall back to `allow` + citation and log
  `timeout`; never block on a slow model.

### 8.1 Calibration

Decision-model thresholds are fit locally, never taken from the vendor default. Procedure:
collect ≥200 labeled (entry, change, is_conflict) pairs from the intervention log (human
labels via `/ledger review`) and from the benchmark (§9); choose τ maximizing F1 subject to
false-deny rate ≤2%; store in `calibration.json` with sample size and date; refit when ≥100
new labels accumulate. If the decision model is unavailable, Tier 2 is skipped and Tier 3
runs on Tier 0/1 flags only.

### 8.2 Decision-model adapter

`DecisionModel` interface: `noul(statement: str, context: str) -> (p_true: float, latency_ms)`,
`choice(options, context) -> (index, p)`. Implementations: `JevDecisionModel` (TypeSafe
System One; endpoint and schema to be confirmed in the first build session, since only the
host and the path prefix `/api/v1/systemone` are known) and `LLMDecisionModel` (Haiku-class,
verbalized probability) as fallback. Both are logged with the same fields so calibration is
per-implementation.

## 9. DecisionDrift-Bench

Purpose: measure what v0.1 never measured. Inputs: scripted sessions (synthetic first, then
recorded real sessions) that seed N decisions/constraints with known falsifiers, then present
"tempting" tasks that invite violating them, with forced compaction every M turns
(`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` or `/compact`). Run under the Agent SDK (TypeScript) so
`PostCompact` and `PostToolBatch` are available and `SDKCompactBoundaryMessage` marks
compactions.

Arms: (a) no monitor; (b) Claude Code auto-memory only; (c) pinning only (§7);
(d) pinning + gate (§8). Metrics: violation rate per decision after 0/1/2/4 compactions,
constraint retention %, false-deny rate, interventions per task, p50/p95 gate latency, monitor
cost as % of main-agent cost, task success (does the tempting task still complete?). Report
with CIs over ≥5 seeds; keep per-run JSON as v0.1 did.

## 10. Phases and exit criteria

| Phase | Build | Exit criterion |
|---|---|---|
| 0 (1 day) | Plugin skeleton, `hooks.json`, transcript reader with fixtures, storage layout, real embedder, delete v0.1 monitor/regex/benchmark | Reader parses fixtures from two Claude Code versions; hooks fire in a smoke session and log |
| 1 (week 1) | Extractor, ledger index, `PreCompact`/`SessionEnd` capture, `PostCompact` audit, `SessionStart` pinning + briefing, `/ledger` command | Retention ≥95% on a 3-compaction scripted session; briefing ≤10 KB; zero main-agent tool calls required |
| 2 (weeks 2–3) | Gate Tiers 0–3, resample flow, provenance force, harm budget, intervention log, `Stop` turn check, decision-model adapter + calibration | On the bench: violation rate ≤5% after 4 compactions with false-deny ≤2%; unflagged p95 ≤800 ms |
| 3 (week 4) | DecisionDrift-Bench with four arms, cost accounting, report | Published numbers with CIs; gate arm beats pinning-only on violation rate without hurting task success |
| 4 | Subagent/agent-team injection and checks, dependency graph + orphan surfacing, mod panel, optional MCP tools, TS Agent SDK packaging | Teams smoke test; panel shows retention and last interventions |

## 11. Open questions (resolve in build, do not block Phase 0–1)

1. Jev API endpoint, request schema, rate limits, and whether context length suits diff
   excerpts (expect to truncate to ~2 KB).
2. Whether `MEMORY.md` mirroring is worth it versus relying on `SessionStart[compact]` alone.
3. `SessionEnd` 1.5 s budget: capture-only there and defer extraction to the next
   `SessionStart`, or raise the timeout.
4. Dependency extraction quality: does the extractor produce useful `depends_on` edges, or
   should they be inferred at supersession time by a targeted query?
5. Agent SDK Python lacks `SessionStart`/`PostCompact`/`PostToolBatch`; support Python only via
   settings-file hooks (`setting_sources=["project"]`).
6. Research track (not v2): JEPA-style latent surprise over the decision manifold; confidence
   carried with entries across handoffs (confidence-laundering mitigation).

## 12. References (headline figures from abstracts; full texts were not reachable from this environment)

Lost in Compaction 2608.11242 · Compact-Bench 2610.31710 · Governance Decay 2606.22528 ·
Slipstream 2605.08580 · STALE/CUPMem 2605.06527 · Remember the Decision 2605.10870 ·
LivePlan 2608.06701 · PAUSE 2610.29870 · REFLEX with Jev 2609.26532 · AI Control 2312.06942 ·
Ctrl-Z 2504.10374 · Factor(U,T) 2512.14745 · Last Step Matters 2608.29685 · VoS 2610.09115 ·
The LLM Proposes, the Executive Disposes 2608.04066 · Inherited goal drift 2603.03258 ·
Claude Code hooks https://code.claude.com/docs/en/hooks · memory https://code.claude.com/docs/en/memory
