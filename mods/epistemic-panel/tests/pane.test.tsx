import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'epistemic-panel',
  component: 'Pane',
  requestId: 'epistemic',
  props: { title: 'Epistemic monitor', isFocused: false, bodyColumns: 70, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const

const LOG = [
  { ts: '2026-10-28T03:28:40Z', kind: 'session_start', source: 'startup', active_entries: 5 },
  { ts: '2026-10-28T03:28:47Z', kind: 'compaction_audit', retention: 1.0, dropped: [], pinned_chars: 346, audited: 5 },
  { ts: '2026-10-28T03:29:01Z', kind: 'gate', decision: 'deny', tool: 'Edit', path: 'src/legacy/pager.py',
    scores: { 'C-0001': { p_conflict: 0.93, ms: 210, input_tokens: 1500 } }, advisor: { verdict: 'conflict', ms: 8100, input_tokens: 4000, output_tokens: 120 } },
  { ts: '2026-10-28T03:29:30Z', kind: 'extraction', added: 2, llm_latency_ms: 2200, input_tokens: 9000 },
].map(r => JSON.stringify(r)).join('\n') + '\n'
const PINNED = '## Active decisions\n- **C-0001** [constraint, user (confirmed)]: x\n- **D-0002** [decision, user (confirmed)]: y\n- **D-0003** [decision, agent]: z\n- **D-0004** [decision, agent]: w\n- **P-0005** [process, user (confirmed)]: v\n'
const STATUS = JSON.stringify({ components: {
  decision_model: { provider: 'typesafe', model: 'jev-latest' }, advisor: { provider: 'claude-cli', model: 'claude-opus-5-5' },
  extractor: { provider: 'openrouter', model: 'anthropic/claude-haiku-5.5' }, embeddings: { provider: 'none', model: '' } }, gate: { enforce: true, deny_budget: 5 } })

function engine(on: Parameters<Parameters<typeof test>[1]>[1], files: Record<string, string>) {
  on('command.register', () => ({ value: { command: 'epistemic' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/me' : undefined }))
  on('session.cwd', () => ({ value: '/work/app' }))
  on('clock.now', () => ({ value: 1 }))
  on('clock.every', () => ({ value: {} }))
  on('fs.read', (_, e) => {
    const text = files[e.path]
    if (text === undefined) throw new Error(`missing ${e.path}`)
    return { value: text }
  })
}

test('the pane draws the core, the present bots and the cost', async ($, on) => {
  const dir = '/home/me/.claude/projects/-work-app/epistemic'
  engine(on, { [`${dir}/interventions.jsonl`]: LOG, [`${dir}/pinned.md`]: PINNED, [`${dir}/status.json`]: STATUS })
  await $.session.start({ cwd: '/work/app', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'epistemic' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /^strength/ }))?.text).toBe('strength 3/6 · 3 of 5 signed off by you')
    expect((await ui.find({ type: 'Text', text: /● ● ● ○ ○/ }))).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /RELEASED TO SERVICE/ }))?.text).toBe('▲ RELEASED TO SERVICE')
    expect((await ui.find({ type: 'Text', text: /^1\/5$/ }))).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /CAPT OPUS/ }))).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /AVIONICS JEV/ }))).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /F\/O HAIKU/ }))).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /RADAR/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /^Jev / }))?.text).toBe('Jev 6 calls · 210 ms · 1.5K tok · ~$0.0001  |  advisor 1 calls · 8.1 s · 4.1K tok  |  extractor 1 calls · 2.2 s · 9.0K tok')
    await ui.unmount()
  }
})

test('absent components draw no bot; embeddings icon appears with openrouter', async ($, on) => {
  const dir = '/home/me/.claude/projects/-work-app/epistemic'
  const status = JSON.stringify({ components: { decision_model: { provider: 'typesafe', model: 'jev-latest' },
    advisor: { provider: 'openrouter', model: 'anthropic/claude-opus-5.5' }, extractor: { provider: 'openrouter', model: 'anthropic/claude-haiku-5.5' },
    embeddings: { provider: 'openrouter', model: 'openai/text-embedding-3-small' } }, gate: { enforce: true, deny_budget: 5 } })
  const log = [{ ts: '2026-10-28T03:28:40Z', kind: 'session_start', active_entries: 1 },
    { ts: '2026-10-28T03:28:41Z', kind: 'component_unavailable', component: 'decision_model', error: 'HTTP 401' }].map(r => JSON.stringify(r)).join('\n') + '\n'
  engine(on, { [`${dir}/interventions.jsonl`]: log, [`${dir}/pinned.md`]: '- **D-0001** [decision, agent]: a\n', [`${dir}/status.json`]: status })
  await $.session.start({ cwd: '/work/app', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'epistemic' })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /AVIONICS JEV/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /CAPT OPUS/ })).toBeDefined()
  expect((await ui.find({ type: 'Text', text: /RADAR/ }))?.text).toBe('  ⊕  RADAR embeddings')
  expect((await ui.find({ type: 'Text', text: /^strength/ }))?.text).toBe('strength 1/6 · 0 of 1 signed off by you')
  await ui.unmount()
})

test('without data the pane says where it looked', async ($, on) => {
  engine(on, {})
  await $.session.start({ cwd: '/work/app', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'epistemic' })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /No epistemic/ }))?.text).toBe('No epistemic monitor data for this project yet.')
  await ui.unmount()
})
