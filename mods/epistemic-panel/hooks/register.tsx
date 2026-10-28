import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Intervention, Snapshot } from '../types'

const PANE = 'epistemic'
const TITLE = 'Epistemic monitor'
const REFRESH_MS = 3000
const snapshot = atom({ plugin: 'epistemic-panel', key: 'snapshot' } as const, null, { shape: 'snapshot-1' })

// The plugin's data directory: EPISTEMIC_DATA_DIR, else Claude Code's project folder for the session's
// working directory (the same rule src/epistemic_monitor/config.py applies).
async function dataDir($: EngineInterface): Promise<string> {
  const override = await $.env.get('EPISTEMIC_DATA_DIR')
  if (override) return override
  const cwd = await $.session.cwd()
  const home = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${(await $.env.get('HOME')) ?? ''}/.claude`
  return `${home}/projects/${cwd.replace(/[^A-Za-z0-9]/g, '-')}/epistemic`
}

function describe(r: Record<string, unknown>): Intervention {
  const ts = String(r.ts ?? '').slice(11, 19)
  const kind = String(r.kind ?? '')
  let detail = ''
  let isDeny = false
  if (kind === 'gate') {
    detail = `${r.decision} ${r.tool}${r.path ? ' ' + String(r.path).split('/').pop() : ''}`
    isDeny = r.decision === 'deny'
  } else if (kind === 'compaction_audit') {
    const ret = typeof r.retention === 'number' ? Math.round(r.retention * 100) + '%' : '?'
    detail = `retention ${ret}, ${Array.isArray(r.dropped) ? r.dropped.length : 0} re-pinned`
  } else if (kind === 'stop' || kind === 'subagent_stop') {
    detail = r.blocked ? 'blocked: contradicts the ledger' : `turn check ${typeof r.turn_check_p === 'number' ? r.turn_check_p : 'skipped'}`
    isDeny = Boolean(r.blocked)
  } else if (kind === 'batch') {
    detail = r.reinjected ? 're-injected after compaction' : 'watch ok'
  } else if (kind === 'supersede' || kind === 'supersede_refused') {
    detail = `${r.entry} by ${r.by}${kind === 'supersede_refused' ? ' (refused)' : ''}`
    isDeny = kind === 'supersede_refused'
  } else if (kind === 'extraction') {
    detail = `${r.added ?? 0} entries`
  } else {
    detail = String(r.event ?? r.source ?? '')
  }
  return { ts, kind, detail, isDeny }
}

const SHOWN = new Set(['gate', 'compaction_audit', 'stop', 'subagent_stop', 'batch', 'supersede', 'supersede_refused',
  'extraction', 'session_start', 'capture', 'review_resolved', 'subagent_start'])

async function takeSnapshot($: EngineInterface): Promise<Snapshot> {
  const dir = await dataDir($)
  const base: Snapshot = { dataDir: dir, isPresent: false, active: 0, review: 0, retention: null, lastAudit: '',
    pinnedChars: 0, denies: 0, blocks: 0, recent: [], readAt: await $.clock.now() }
  let text = ''
  try {
    text = await $.fs.read(`${dir}/interventions.jsonl`)
  } catch {
    return base
  }
  const rows: Record<string, unknown>[] = []
  for (const line of text.split('\n').slice(-400)) {
    if (!line.startsWith('{')) continue
    try { rows.push(JSON.parse(line) as Record<string, unknown>) } catch { /* a partial last line */ }
  }
  const out = { ...base, isPresent: true }
  for (const r of rows) {
    if (r.kind === 'compaction_audit') {
      out.retention = typeof r.retention === 'number' ? r.retention : null
      out.lastAudit = String(r.ts ?? '').slice(11, 19)
      out.pinnedChars = Number(r.pinned_chars ?? 0)
    }
    if (r.kind === 'gate' && r.decision === 'deny') out.denies += 1
    if ((r.kind === 'stop' || r.kind === 'subagent_stop') && r.blocked) out.blocks += 1
    if (r.kind === 'session_start' || r.kind === 'extraction') {
      if (typeof r.active_entries === 'number') out.active = r.active_entries
      if (typeof r.active === 'number') out.active = r.active
    }
  }
  try {
    const pinned = await $.fs.read(`${dir}/pinned.md`)
    const bullets = pinned.split('\n').filter(l => /^- \*\*[A-Z]+-\d+\*\*/.test(l)).length
    if (bullets) out.active = bullets
    out.review = pinned.split('\n').filter(l => /Needs review/.test(l)).length ? pinned.split('\n').filter(l => /\(depends on|shares scope/.test(l)).length : 0
  } catch { /* no pinned block yet */ }
  out.recent = rows.filter(r => SHOWN.has(String(r.kind))).slice(-12).map(describe)
  return out
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'epistemic', description: 'Open the epistemic monitor pane (ledger retention and interventions)' })
    await update($, snapshot, () => null)
    const refresh = async () => { const s = await takeSnapshot($); await update($, snapshot, () => s) }
    void refresh()
    $.clock.every(REFRESH_MS, () => { void refresh() })
    if (e.surface !== 'terminal') void $.ui.open({ id: PANE, title: TITLE })
    return next(e)
  })

  on('command.run', { command: 'epistemic' }, async $ => {
    const s = await takeSnapshot($)
    await update($, snapshot, () => s)
    await $.ui.open({ id: PANE, title: TITLE })
    return { text: 'Epistemic monitor pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s = await read($, snapshot)
    if (!s) return <Text dimColor>Reading the ledger…</Text>
    if (!s.isPresent) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No epistemic monitor data for this project yet.</Text>
          <Text dimColor>{s.dataDir}</Text>
        </Box>
      )
    }
    const retention = s.retention === null ? 'no compaction yet' : `${Math.round(s.retention * 100)}% at ${s.lastAudit}`
    const room = Math.max(1, (e.viewport?.rows ?? 24) - 7)
    return (
      <Box flexDirection="column">
        <Text bold>Ledger: {s.active} active{s.review ? `, ${s.review} need review` : ''}</Text>
        <Text>Retention: {retention}{s.pinnedChars ? ` (${s.pinnedChars} chars pinned)` : ''}</Text>
        <Text>Interventions: {s.denies} denies, {s.blocks} turn blocks</Text>
        <Text dimColor>Last events</Text>
        {s.recent.length === 0 && <Text dimColor>none yet</Text>}
        {s.recent.slice(-room).map(r => (
          <Text color={r.isDeny ? 'red' : undefined} dimColor={!r.isDeny}>
            {r.ts} {r.kind} {r.detail}
          </Text>
        ))}
      </Box>
    )
  })
}
