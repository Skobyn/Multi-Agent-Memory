import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'epistemic-panel',
  component: 'Pane',
  requestId: 'epistemic',
  props: { title: 'Epistemic monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const

const LOG = [
  { ts: '2026-10-28T03:28:47Z', kind: 'session_start', source: 'compact', active_entries: 3 },
  { ts: '2026-10-28T03:28:47Z', kind: 'compaction_audit', retention: 0.67, dropped: ['C-0001'], pinned_chars: 346 },
  { ts: '2026-10-28T03:29:01Z', kind: 'gate', decision: 'deny', tool: 'Edit', path: 'src/legacy/pager.py' },
  { ts: '2026-10-28T03:29:30Z', kind: 'stop', blocked: true },
].map(r => JSON.stringify(r)).join('\n') + '\n'
const PINNED = '## Active decisions\n- **C-0001** [constraint, user]: x\n- **D-0002** [decision, user]: y\n- **D-0003** [decision, agent]: z\n'

function engine(on: Parameters<Parameters<typeof test>[1]>[1], files: Record<string, string>) {
  on('command.register', () => ({ value: { command: 'epistemic' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/me' : undefined }))
  on('session.cwd', () => ({ value: '/work/app' }))
  on('clock.now', () => ({ value: 1 }))
  on('clock.every', () => ({ value: { cancel: () => undefined } }))
  on('fs.read', (_, e) => {
    const text = files[e.path]
    if (text === undefined) throw new Error(`missing ${e.path}`)
    return { value: text }
  })
}

test('the pane shows retention, counts and the last events', async ($, on) => {
  const dir = '/home/me/.claude/projects/-work-app/epistemic'
  engine(on, { [`${dir}/interventions.jsonl`]: LOG, [`${dir}/pinned.md`]: PINNED })
  await $.session.start({ cwd: '/work/app', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'epistemic' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /Ledger/ }))?.text).toBe('Ledger: 3 active')
    expect((await ui.find({ type: 'Text', text: /Retention/ }))?.text).toBe('Retention: 67% at 03:28:47 (346 chars pinned)')
    expect((await ui.find({ type: 'Text', text: /Interventions/ }))?.text).toBe('Interventions: 1 denies, 1 turn blocks')
    expect((await ui.find({ type: 'Text', text: /deny Edit/ }))?.text).toBe('03:29:01 gate deny Edit pager.py')
    await ui.unmount()
  }
})

test('without data the pane says where it looked', async ($, on) => {
  engine(on, {})
  await $.session.start({ cwd: '/work/app', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'epistemic' })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /No epistemic/ }))?.text).toBe('No epistemic monitor data for this project yet.')
  expect((await ui.find({ type: 'Text', text: /epistemic$/ }))?.text).toBe('/home/me/.claude/projects/-work-app/epistemic')
  await ui.unmount()
})
