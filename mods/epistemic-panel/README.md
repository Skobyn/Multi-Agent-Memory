# epistemic-panel

A Claude Code mod: a pane beside the chat that shows the epistemic monitor's state for the current
project as an aircraft logbook (the plugin's author works in aircraft maintenance and business
intelligence, and the pane leans into both):

- **KPI tiles**: airworthiness (retention at the last compaction), logbook size, open squawks
  (entries that need review), no-go items used against the deny budget, grounded replies (turn
  blocks), flight time and fuel (monitor latency, tokens and the Jev cost).
- **The aircraft**: the ledger itself. Each entry is a window on the fuselage, filled when you signed
  it off (user-confirmed), the hull lengthens as the logbook grows, the tail fin grows with strength,
  and the hull colour is airworthiness: green released to service, yellow conditional with squawks
  open, red grounded after a blocked reply.
- **Crew**: the models actually on board this session. Captain (advisor, drawn large), First Officer
  (extractor, drawn small), Avionics (Jev decision model), Radar (embeddings). A component that is not
  configured, or that reported itself unavailable, has no figure.
- **Flight log and squawk log**: cost per component and the last interventions, denies in red.

It also keeps a status-line entry (`✈ logbook 8 · airworthy 100% · no-go 1/5`) and raises a toast the
moment a tool call is denied or a reply is sent back. `/epistemic` opens the pane; it refreshes every
three seconds from `interventions.jsonl`, `pinned.md` and `status.json` in the plugin's data directory.

```bash
claude --plugin-dir /path/to/Multi-Agent-Memory --plugin-dir /path/to/Multi-Agent-Memory/mods/epistemic-panel
```

or `/plugin install epistemic-panel@epistemic-monitor` after `/plugin marketplace add Skobyn/Multi-Agent-Memory`.
Validate and type-check with `claude plugin validate mods/epistemic-panel` and `tsc -p mods/epistemic-panel`
once the engine has laid the types beside it; `claude plugin test mods/epistemic-panel` runs its tests.
