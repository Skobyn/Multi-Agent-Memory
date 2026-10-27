"""Epistemic Monitor v2: the enforcement layer for agent memory.

Hooks-first Claude Code plugin that keeps a typed decision ledger outside the
compacted context, audits every compaction against it, and (from Phase 2)
gates tool calls that contradict it. See docs/INTENT.md and docs/DESIGN.md.
"""

__version__ = "2.0.0a0"
