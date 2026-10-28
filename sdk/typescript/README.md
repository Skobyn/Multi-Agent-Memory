# Epistemic Monitor for the Claude Agent SDK

```ts
import { query } from '@anthropic-ai/claude-agent-sdk'
import { epistemicMonitorPlugin, epistemicMonitorHooks } from './sdk/typescript/index.ts'

const root = '/path/to/Multi-Agent-Memory'

// 1. As a plugin (preferred): Claude Code loads hooks/hooks.json and .mcp.json as the CLI does.
for await (const msg of query({ prompt: 'Add currency support to billing', options: { plugins: [epistemicMonitorPlugin(root)] } })) {
  console.log(msg)
}

// 2. As hook callbacks, for hosts that do not load plugins.
for await (const msg of query({ prompt: '...', options: { hooks: epistemicMonitorHooks({ root }) } })) {
  console.log(msg)
}
```

Either way the session gets the full loop: ledger briefing at `SessionStart`, extraction at
`UserPromptSubmit`/`PreCompact`/`SessionEnd`, the audit and re-pin at `PostCompact`, the gate at
`PreToolUse`, the behavior watch at `PostToolBatch`, and the turn checks at `Stop`/`SubagentStop`.
Python 3.10+ must be on `PATH`; the hook processes read `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY` or
`ANTHROPIC_API_KEY`, and any `EPISTEMIC_*` overrides from the environment (DESIGN §12).

Type-check: `tsc -p sdk/typescript` with `@anthropic-ai/claude-agent-sdk` and `@types/node` installed.
