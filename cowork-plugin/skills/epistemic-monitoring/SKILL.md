---
name: epistemic-monitoring
description: Automatic contradiction detection and confidence scoring against a persistent archive. Monitors assistant outputs in real-time, detects when the agent contradicts prior decisions, and interrupts before damage is done.
---

# Epistemic Monitoring

This skill makes Claude a self-correcting agent that catches its own mistakes by checking every output against a persistent archive of prior decisions.

## The Problem

```
Session 1: "We chose PostgreSQL for the database."
   ... 200 turns later, context compaction happens ...
Session 5: "Let's use MongoDB for the database."

Without monitoring: Agent proceeds confidently. Nobody notices the contradiction.
With monitoring:    HARD INTERRUPT - "This contradicts Session 1, Turn 4:
                    'chose PostgreSQL'. Query archive: database decisions"
```

## How It Works

```
Assistant produces output
       |
       v
 memory_observe (records turn)
       |
       v
 Contradiction check against archived decisions
       |
       +---> Contradiction found? --> HARD INTERRUPT (always)
       |
       v
 Confidence scoring (0.0-1.0)
       |
       +---> Score < 0.5? --> HARD INTERRUPT with citation
       +---> Score < 0.7? --> Soft nudge: "Consider querying archive"
       +---> Score > 0.7? --> No interrupt, proceed normally
```

## Automatic Behavior

When this skill is active, Claude MUST follow this protocol on every assistant output:

### 1. Observe Every Turn

After producing a response, call `memory_observe` with:
- `role`: "assistant"
- `content`: the full response text
- `session_id`: current session identifier

Check the returned result for contradiction alerts.

### 2. Handle Interrupts

**Hard Interrupt (contradiction or score < 0.5):**
- STOP the current response
- Show the user both statements side-by-side:
  ```
  CONTRADICTION DETECTED
  Current:  "[what you just said]"
  Archived: "[what was decided before]" (Session X, Turn Y)

  Querying archive for full context...
  ```
- Call `memory_search` with the suggested query
- Revise the response using the archived context

**Soft Nudge (score 0.5-0.7):**
- Append a note to the response:
  ```
  Note: This topic has prior context in the archive that may be relevant.
  Use /memory:search [topic] to review.
  ```

**No Interrupt (score > 0.7):**
- Proceed normally. The output is well-grounded.

### 3. Record User Turns Too

When the user sends a message, call `memory_observe` with:
- `role`: "user"
- `content`: the user's message

This builds the full conversation record for future reference.

## Confidence Score Components

| Signal | Score Impact | What It Means |
|--------|-------------|---------------|
| Entity gap | -0.1 each | Output references something from the archive that's missing from current context |
| Topic drift | -0.05 each | Archive has history on this topic but current context doesn't |
| Contradiction | -0.2 each | Output conflicts with an archived decision |

## Thresholds

| Score Range | Action |
|-------------|--------|
| > 0.7 | No interrupt. Output is well-grounded. |
| 0.5 - 0.7 | Soft nudge: suggest querying archive |
| < 0.5 | Hard interrupt: specific citation with conflicting text |
| Any contradiction | Hard interrupt regardless of score |

## What Gets Detected

- **Antonym contradictions**: "use X" vs "don't use X"
- **Choice reversals**: "chose PostgreSQL" vs "decided on MongoDB"
- **Negation asymmetry**: "approved the design" vs "rejected the design"
- **Entity gaps**: Referencing something archived but not in current context
- **Topic drift**: Making claims about a topic with archived history you haven't consulted

## When This Fires Automatically

This monitoring happens on every assistant turn. No user action is required.
The skill is always-on when the plugin is active.
