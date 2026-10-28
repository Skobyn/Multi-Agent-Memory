/**
 * Epistemic Monitor for the Claude Agent SDK (TypeScript).
 *
 * The plugin is hooks-first: everything it does runs as `python3 -I <root>/bin/em-hook hook <event>`
 * on Claude Code's hook events. Two ways to attach it to an SDK session:
 *
 *   1. `epistemicMonitorPlugin(root)` — hand the plugin directory to the SDK's `plugins` option.
 *      Claude Code loads hooks/hooks.json (and .mcp.json) exactly as the CLI does. Preferred.
 *
 *   2. `epistemicMonitorHooks(root)` — SDK hook callbacks that spawn the same hook commands and
 *      return their JSON. For hosts that do not load plugins, or that want the hook calls visible
 *      in their own process.
 *
 * Both need Python 3.10+ on PATH and the environment variables the plugin reads
 * (TYPESAFE_API_KEY, OPENROUTER_API_KEY or ANTHROPIC_API_KEY, optional EPISTEMIC_* overrides).
 */
import { spawn } from 'node:child_process'
import type { HookCallback, HookCallbackMatcher, HookEvent, HookInput, HookJSONOutput, SdkPluginConfig } from '@anthropic-ai/claude-agent-sdk'

/** Hook event -> em-hook subcommand, as hooks/hooks.json wires them. */
const EVENTS: Record<string, { event: string; matcher?: string; timeout: number; async?: boolean }> = {
  SessionStart: { event: 'session_start', timeout: 5 },
  SessionEnd: { event: 'session_end', timeout: 2 },
  UserPromptSubmit: { event: 'user_prompt_submit', timeout: 30, async: true },
  PreToolUse: { event: 'pre_tool_use', matcher: 'Edit|Write|MultiEdit|NotebookEdit|Bash', timeout: 25 },
  PostToolUse: { event: 'post_tool_use', matcher: 'Edit|Write|MultiEdit|NotebookEdit|Bash', timeout: 10, async: true },
  PostToolBatch: { event: 'post_tool_batch', timeout: 5 },
  Stop: { event: 'stop', timeout: 25 },
  PreCompact: { event: 'pre_compact', timeout: 60 },
  PostCompact: { event: 'post_compact', timeout: 15 },
  SubagentStart: { event: 'subagent_start', timeout: 5 },
  SubagentStop: { event: 'subagent_stop', timeout: 25 },
}

export type EpistemicMonitorOptions = {
  /** Absolute path of the Multi-Agent-Memory checkout (the plugin root). */
  root: string
  /** Python executable; default `python3`. */
  python?: string
  /** Extra environment for the hook processes (EPISTEMIC_* overrides, API keys). */
  env?: Record<string, string>
}

/** The plugin as the SDK's `plugins` entry: `query({ prompt, options: { plugins: [epistemicMonitorPlugin(root)] } })`. */
export function epistemicMonitorPlugin(root: string): SdkPluginConfig {
  return { type: 'local', path: root }
}

/** Run one hook command with the event payload on stdin and parse its JSON output (empty output = no decision). */
export function runHook(opts: EpistemicMonitorOptions, event: string, input: HookInput, signal?: AbortSignal): Promise<HookJSONOutput> {
  return new Promise(resolve => {
    const child = spawn(opts.python ?? 'python3', ['-I', `${opts.root}/bin/em-hook`, 'hook', event], {
      env: { ...process.env, CLAUDE_PLUGIN_ROOT: opts.root, ...(opts.env ?? {}) },
      stdio: ['pipe', 'pipe', 'ignore'],
    })
    let out = ''
    child.stdout.on('data', (chunk: Buffer) => { out += chunk.toString() })
    const done = () => {
      const text = out.trim()
      if (!text) return resolve({})
      try { resolve(JSON.parse(text) as HookJSONOutput) } catch { resolve({}) }
    }
    child.on('close', done)
    child.on('error', () => resolve({}))
    signal?.addEventListener('abort', () => child.kill())
    child.stdin.end(JSON.stringify(input))
  })
}

/** SDK `hooks` option that runs the plugin's hook commands: `query({ prompt, options: { hooks: epistemicMonitorHooks({ root }) } })`. */
export function epistemicMonitorHooks(opts: EpistemicMonitorOptions): Partial<Record<HookEvent, HookCallbackMatcher[]>> {
  const hooks: Partial<Record<HookEvent, HookCallbackMatcher[]>> = {}
  for (const [name, spec] of Object.entries(EVENTS)) {
    const callback: HookCallback = async (input, _toolUseID, { signal }) => {
      if (spec.async) {
        void runHook(opts, spec.event, input)   // fire and forget, as hooks.json marks it
        return {}
      }
      return runHook(opts, spec.event, input, signal)
    }
    const matcher: HookCallbackMatcher = { hooks: [callback], timeout: spec.timeout }
    if (spec.matcher) matcher.matcher = spec.matcher
    hooks[name as HookEvent] = [matcher]
  }
  return hooks
}
