# epistemic-panel

A Claude Code mod: a pane beside the chat that shows the epistemic monitor's state for the
current project, refreshed every three seconds from `interventions.jsonl` and `pinned.md`:
active entries and review items, retention at the last compaction, deny and turn-block counts,
and the last interventions. `/epistemic` opens it.

```bash
claude --plugin-dir /path/to/Multi-Agent-Memory --plugin-dir /path/to/Multi-Agent-Memory/mods/epistemic-panel
```

It reads the same data directory as the plugin (`EPISTEMIC_DATA_DIR`, else
`~/.claude/projects/<project-slug>/epistemic/`). Validate and type-check with
`claude plugin validate mods/epistemic-panel` and `tsc -p mods/epistemic-panel` once the engine has
laid the types beside it.
