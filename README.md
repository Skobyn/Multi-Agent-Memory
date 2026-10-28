# Epistemic Monitor

**The enforcement layer for agent memory.**

Long-running agent sessions do not fail because context is lost. They fail because the agent
does not know what it lost, and nothing checks its next action against what it previously
committed to. Compaction summaries keep roughly a sixth of the constraints a user stated;
violations climb to about a third after one compaction and three quarters after four.
Memory products remember. This one holds the line.

Epistemic Monitor is a hooks-first Claude Code plugin that:

1. keeps a **typed decision ledger** (decisions, constraints, premises, commitments, required
   process steps) outside the compacted context, with provenance and machine-checkable falsifiers;
2. **audits every compaction** against the ledger and re-pins what the summary dropped;
3. **gates tool calls** that contradict the ledger, cheaply (deterministic checks, then a typed
   decision model, then an LLM advisor only on a flag), asking the agent to revise or to supersede
   the decision explicitly before anything is denied.

It runs whether or not the main agent cooperates, and it can deny, rewrite, or redirect, not
merely advise. Read [`docs/INTENT.md`](docs/INTENT.md) for the why and
[`docs/DESIGN.md`](docs/DESIGN.md) for the build-ready design.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Plugin skeleton, hooks wired and logging, transcript reader, ledger store, embeddings and decision-model adapters, Tier 0 gate (advisory), Bash write classifier | **done** |
| 1 | Extractor (incremental on each prompt, full pass at compaction, background after Stop and SessionEnd), Jev entailment audit, post-compaction behavior watch, `/ledger` command, three-compaction exit check | **done** |
| 2 | Gate Tiers 1–3 (retrieval, Jev conflict + severity with per-type thresholds and near-threshold resampling, advisor with the session's recent tool calls), process-order falsifier, resample-before-deny with budgets and caps, calibration from labels, Stop turn check, enforcement on, live gate bench | **done** |
| 3 | DecisionDrift-Bench over real Claude Code sessions (four arms, forced compaction, deterministic graders, hard triggers), results published | **done**, exit criterion partly met (see below) |
| 4 | Subagent/agent-team checks, dependency graph, panel, TS Agent SDK packaging | |

The v0.1 prototype (regex monitor, hash embeddings, voluntary tool calls) was removed; its spec is
archived at [`docs/archive/v0.1-spec.md`](docs/archive/v0.1-spec.md).

## Install (Claude Code)

No dependencies beyond Python 3.10+.

```bash
git clone https://github.com/Skobyn/Multi-Agent-Memory
claude --plugin-dir /path/to/Multi-Agent-Memory
```

The plugin registers hooks for `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `PostToolBatch`, `Stop`, `PreCompact`, `PostCompact`, `SubagentStart` and
`SubagentStop` (see [`hooks/hooks.json`](hooks/hooks.json)). Everything it writes lives beside
Claude Code's own auto-memory for the project:

```
~/.claude/projects/<project-slug>/epistemic/
  ledger.jsonl          append-only event log (source of truth)
  index.db              SQLite projection: entries, FTS5, embeddings, watermarks
  interventions.jsonl   every hook run, audit, gate decision
  pinned.md             the block re-injected after each compaction
  transcripts/          verbatim captures taken at PreCompact and SessionEnd
```

Check it is alive, and inspect or edit the ledger (also available as `/ledger` inside Claude Code):

```bash
python3 -I bin/em-hook status
python3 -I bin/em-hook ledger list
python3 -I bin/em-hook ledger add --type constraint --statement "Never send email on my behalf; draft only." --user
```

What happens during a session: every prompt you send is scanned for durable decisions and constraints
(a Haiku-class call that never delays the turn); before each compaction the transcript is captured and
fully extracted; after each compaction every ledger entry is checked against the summary, first by
literal anchors, then by a Jev yes/no, and whatever the summary dropped is re-injected verbatim; the
first three tool batches after a compaction are checked for conflicts with the pinned entries.

## Configuration

One JSON file, `~/.claude/projects/<slug>/epistemic/config.json`, with env overrides such as
`EPISTEMIC_GATE_ENFORCE=false`. Defaults: TypeSafe Jev as the decision model, Haiku as the
extractor, Opus as the advisor, OpenRouter embeddings. The advisor model needs an account that can
pay for it: OpenRouter pre-checks each request against prepaid credits, so with a zero balance Opus
and Sonnet requests are refused (HTTP 402) while Haiku passes; set `EPISTEMIC_ADVISOR_MODEL` or add
credits. Secrets come only from `TYPESAFE_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`. Full schema in
[`docs/DESIGN.md` §12](docs/DESIGN.md#12-configuration).

## Supersession

A decision the user made can only be changed by the user. Either side changes a ledger entry
with one line in a message:

```
SUPERSEDE D-0042: moving to MongoDB because the data is document-shaped
```

From the agent, that line retires only entries the agent itself made; from the user, it retires
anything, and entries that depended on it are flagged for review.

## Development

```bash
pip install -e .[dev]
pytest -q                      # unit tests, offline
EM_LIVE_TESTS=1 pytest -q -k live   # one live call to the decision model
bash scripts/smoke.sh          # drive every hook with sample payloads (offline)
EPISTEMIC_DATA_DIR=/tmp/p1 python3 -I scripts/phase1_check.py   # live three-compaction exit check
```

On 2026-10-27 the exit check recorded extraction recall 100% at each of three compactions; at the
third, a 150-word generic summary retained 0 of 12 seeded items, the audit flagged all 12, and the
pin block re-injected them (retained 100%). PreCompact took 7–13 s, PostCompact under 1 s.

```bash
EPISTEMIC_DATA_DIR=/tmp/p2 python3 -I scripts/phase2_check.py   # live gate bench (Phase 2 exit check)
```

On 2026-10-27 the gate bench (12 user-backed entries, 36 labeled tool calls, half used to calibrate,
half held out) recorded on the held-out half: violation rate 0%, false-deny rate 0%, unflagged-path
p95 560 ms, flagged-path p95 3.5 s with the advisor on Haiku. Results in `docs/results/`.

```bash
python3 -I scripts/decisiondrift_bench.py --seeds 3 --parallel 6 --out results.json --report REPORT.md
python3 -I scripts/decisiondrift_bench.py --trigger hard --arms none,pin,gate --levels c1 --seeds 3   # ConstraintRot-style
```

DecisionDrift-Bench runs every cell as a real `claude -p` session in a throwaway project with one
forced compaction and grades violations deterministically. On 2026-10-28 (Haiku 5.5 executor, 264
sessions): with hard triggers the no-plugin baseline violated a stated policy in 22% of tempting
sessions [9%, 45%]; the pinning arm and the gate arm violated in 0% [0%, 18%], with no false denies
and no completion loss beyond noise. With plain triggers every arm scored 0% violation and 100%
completion, because Claude Code 2.1.311's compaction summary keeps rules the user stated. The exit
criterion's "gate beats pinning" clause is therefore a tie, not a win; the gate's one deny in 72
hard-trigger sessions stopped an agent that had started to violate despite the briefing. The bench
also exposed and fixed two gate defects (false denies on compliant commits). Full write-up:
[`docs/results/DECISIONDRIFT.md`](docs/results/DECISIONDRIFT.md).

## Enforcement

The gate is on by default and budgeted. A tool call is denied only when it is **hard**: it trips a
deterministic falsifier, or a user-backed entry is judged in conflict by the decision model and
confirmed by the advisor (or, without calibration, scored at 0.9 or above). Agent-made entries only
produce a citation. Each session gets at most 5 denies, at most 5 consecutive denies on one entry,
and the advisor is called at most once per 5 tool calls per entry. The deny message always cites the
entry and offers the two ways forward: revise the change, or `SUPERSEDE <id>: <reason>`.

Calibrate thresholds from your own sessions:

```bash
python3 -I bin/em-hook ledger review          # recent gate decisions with model scores
python3 -I bin/em-hook ledger label <tool_use_id> conflict|ok
python3 -I bin/em-hook ledger mark-good <session_id>   # a session you know was fine
python3 -I bin/em-hook ledger calibrate        # per-type thresholds, false-deny ceiling 2%
```

Transcript fixtures under `tests/fixtures/` cover Claude Code 2.1.311 and the older
`compact_boundary` format; the reader is defensive because the format is undocumented.
