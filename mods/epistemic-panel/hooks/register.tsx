import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Component, Intervention, Snapshot } from '../types'

const PANE = 'epistemic'
const TITLE = 'Epistemic monitor'
const REFRESH_MS = 3000
const JEV_USD_PER_MTOK = 0.042
const snapshot = atom({ plugin: 'epistemic-panel', key: 'snapshot' } as const, null, { shape: 'snapshot-2' })

// The plugin's data directory: EPISTEMIC_DATA_DIR, else Claude Code's project folder for the session's
// working directory (the same rule src/epistemic_monitor/config.py applies).
async function dataDir($: EngineInterface): Promise<string> {
  const override = await $.env.get('EPISTEMIC_DATA_DIR')
  if (override) return override
  const cwd = await $.session.cwd()
  const home = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${(await $.env.get('HOME')) ?? ''}/.claude`
  return `${home}/projects/${cwd.replace(/[^A-Za-z0-9]/g, '-')}/epistemic`
}

type Row = Record<string, unknown>

function describe(r: Row): Intervention {
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
  'extraction', 'session_start', 'capture', 'review_resolved', 'subagent_start', 'ledger_add'])

const n = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)

async function takeSnapshot($: EngineInterface): Promise<Snapshot> {
  const dir = await dataDir($)
  const base: Snapshot = { dataDir: dir, isPresent: false, active: 0, confirmed: 0, review: 0, retention: null, retentions: [],
    lastAudit: '', pinnedChars: 0, denies: 0, blocks: 0, denyBudget: 5, components: [], recent: [], readAt: await $.clock.now() }
  let text = ''
  try {
    text = await $.fs.read(`${dir}/interventions.jsonl`)
  } catch {
    return base
  }
  const rows: Row[] = []
  for (const line of text.split('\n').slice(-600)) {
    if (!line.startsWith('{')) continue
    try { rows.push(JSON.parse(line) as Row) } catch { /* a partial last line */ }
  }
  const out: Snapshot = { ...base, isPresent: true }

  // components: the roster the plugin wrote at session start, minus anything reported unavailable since
  const roles: Component['role'][] = ['decision_model', 'advisor', 'extractor', 'embeddings']
  const comps = new Map<Component['role'], Component>()
  try {
    const st = JSON.parse(await $.fs.read(`${dir}/status.json`)) as Row
    const cs = (st.components ?? {}) as Record<string, Row>
    out.denyBudget = n((st.gate as Row | undefined)?.deny_budget) || 5
    for (const role of roles) {
      const c = cs[role]
      if (!c) continue
      const provider = String(c.provider ?? '')
      comps.set(role, { role, provider, model: String(c.model ?? ''), isPresent: provider !== '' && provider !== 'none', calls: 0, ms: 0, tokens: 0 })
    }
  } catch { /* no roster yet: the first session start writes it */ }
  const unavailable = new Set(rows.filter(r => r.kind === 'component_unavailable').map(r => String(r.component)))
  for (const [role, c] of comps) if (unavailable.has(role)) c.isPresent = false

  const tally = (role: Component['role'], calls: number, ms: number, tokens: number) => {
    const c = comps.get(role)
    if (c) { c.calls += calls; c.ms += ms; c.tokens += tokens }
  }
  for (const r of rows) {
    if (r.kind === 'compaction_audit') {
      const ret = typeof r.retention === 'number' ? r.retention : null
      out.retention = ret
      if (ret !== null) out.retentions.push(ret)
      out.lastAudit = String(r.ts ?? '').slice(11, 19)
      out.pinnedChars = n(r.pinned_chars)
      tally('decision_model', n(r.audited), 0, 0)
    }
    if (r.kind === 'gate') {
      if (r.decision === 'deny') out.denies += 1
      const scores = (r.scores ?? {}) as Record<string, Row>
      for (const s of Object.values(scores)) tally('decision_model', 1, n(s.ms), n(s.input_tokens))
      const adv = r.advisor as Row | undefined
      if (adv && typeof adv.ms === 'number') tally('advisor', 1, n(adv.ms), n(adv.input_tokens) + n(adv.output_tokens))
    }
    if ((r.kind === 'stop' || r.kind === 'subagent_stop')) {
      if (r.blocked) out.blocks += 1
      if (typeof r.turn_check_p === 'number') tally('decision_model', 1, 0, 0)
    }
    if (r.kind === 'extraction') tally('extractor', 1, n(r.llm_latency_ms), n(r.input_tokens))
    if (r.kind === 'session_start' && typeof r.active_entries === 'number') out.active = r.active_entries
  }
  try {
    const pinned = await $.fs.read(`${dir}/pinned.md`)
    const lines = pinned.split('\n')
    const bullets = lines.filter(l => /^- \*\*[A-Z]+-\d+\*\*/.test(l))
    if (bullets.length) out.active = bullets.length
    out.confirmed = bullets.filter(l => /\(confirmed\)|user \(confirmed\)|, user\]|user ✓/.test(l)).length
    out.review = lines.filter(l => /\(depends on|shares scope/.test(l)).length
  } catch { /* no pinned block yet */ }
  out.components = roles.map(r => comps.get(r)).filter((c): c is Component => Boolean(c))
  out.recent = rows.filter(r => SHOWN.has(String(r.kind))).slice(-8).map(describe)
  return out
}

// ---- the aircraft: the ledger as a logbook, each entry a window on the fuselage ------------------
function strengthLevel(entries: number): number {
  return entries <= 0 ? 0 : Math.min(6, 1 + Math.floor(Math.log2(entries)))
}

function aircraftRows(s: Snapshot): string[] {
  const windows = Math.min(s.active, 24)
  if (windows === 0) return ['      (no logbook entries yet: state a decision or constraint and it appears here)']
  const confirmed = Math.min(s.confirmed, windows)
  const cabin = Array.from({ length: windows }, (_, i) => (i < confirmed ? '●' : '○')).join(' ')
  const len = cabin.length + 4
  const fin = '_'.repeat(Math.min(4, 1 + strengthLevel(s.active)))
  const wing = Math.max(3, Math.floor(len / 4))
  return [
    `          ${fin}|\\`,
    `    ______${' '.repeat(fin.length)}|  \\${'_'.repeat(Math.max(0, len - fin.length - 6))}`,
    `   /  ${cabin}    \\___`,
    `  <${'_'.repeat(len)}/`,
    `     ${' '.repeat(Math.floor(wing / 2))}‾\\_/‾${' '.repeat(wing)}‾\\_/‾`,
  ]
}

function fmtMs(ms: number): string { return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms` }
function fmtTok(t: number): string { return t >= 1000 ? `${(t / 1000).toFixed(1)}K tok` : `${t} tok` }

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'epistemic', description: 'Open the epistemic monitor pane (ledger core, components, interventions)' })
    await update($, snapshot, () => null)
    let seen = { denies: 0, blocks: 0 }
    const refresh = async () => {
      const s = await takeSnapshot($)
      await update($, snapshot, () => s)
      if (s.isPresent) {
        const ret = s.retention === null ? '' : ` · retention ${Math.round(s.retention * 100)}%`
        $.ui.status(`✈ logbook ${s.active}${ret.replace('retention', 'airworthy')} · no-go ${s.denies}/${s.denyBudget}${s.review ? ` · ${s.review} squawks` : ''}`)
        if (s.denies > seen.denies) $.ui.toast(`No-go: the epistemic monitor denied a tool call (${s.denies}/${s.denyBudget} this session)`)
        if (s.blocks > seen.blocks) $.ui.toast('Grounded: a reply contradicted the logbook and was sent back')
        seen = { denies: s.denies, blocks: s.blocks }
      }
    }
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
    const level = strengthLevel(s.active)
    const airworthy = s.retention === null ? null : Math.round(s.retention * 100)
    const grounded = s.blocks > 0 || (airworthy !== null && airworthy < 60)
    const hullColor = grounded ? 'red' : s.review > 0 || (airworthy !== null && airworthy < 90) ? 'yellow' : 'green'
    const trend = s.retentions.length > 1 ? s.retentions.map(r => (r >= 0.95 ? '▇' : r >= 0.7 ? '▅' : r >= 0.4 ? '▃' : '▁')).join('') : ''
    const jev = s.components.find(c => c.role === 'decision_model' && c.isPresent)
    const opus = s.components.find(c => c.role === 'advisor' && c.isPresent)
    const haiku = s.components.find(c => c.role === 'extractor' && c.isPresent)
    const emb = s.components.find(c => c.role === 'embeddings' && c.isPresent)
    const short = (m: string) => m.replace(/^anthropic\//, '').replace(/^claude-/, '').replace(/-\d+(\.\d+|-\d+)?$/, '').toUpperCase()
    const flightMs = s.components.reduce((a, c) => a + c.ms, 0)
    const tokens = s.components.reduce((a, c) => a + c.tokens, 0)
    const jevUsd = jev && jev.tokens ? (jev.tokens / 1e6) * JEV_USD_PER_MTOK : 0
    const tiles: [string, string][] = [
      ['AIRWORTHY', airworthy === null ? 'n/a' : `${airworthy}%`],
      ['LOGBOOK', `${s.active}`],
      ['SQUAWKS', `${s.review}`],
      ['NO-GO', `${s.denies}/${s.denyBudget}`],
      ['GROUNDED', `${s.blocks}`],
      ['FLIGHT TIME', fmtMs(flightMs)],
      ['FUEL', tokens ? fmtTok(tokens) + (jevUsd ? ` ~$${jevUsd.toFixed(3)}` : '') : '0'],
    ]
    const cost = s.components.filter(c => c.isPresent && c.calls > 0).map(c => {
      const name = c.role === 'decision_model' ? 'Jev' : c.role
      const usd = c.role === 'decision_model' && c.tokens ? ` · ~$${((c.tokens / 1e6) * JEV_USD_PER_MTOK).toFixed(4)}` : ''
      return `${name} ${c.calls} calls${c.ms ? ' · ' + fmtMs(c.ms) : ''}${c.tokens ? ' · ' + fmtTok(c.tokens) : ''}${usd}`
    })
    const release = s.active === 0 ? '' : grounded ? `▼ GROUNDED: ${s.blocks ? `${s.blocks} reply block(s)` : `airworthiness ${airworthy}%`}`
      : s.review > 0 ? `▲ CONDITIONAL: ${s.review} squawk(s) open (/ledger resolve)` : '▲ RELEASED TO SERVICE'
    const room = Math.max(1, (e.viewport?.rows ?? 34) - 20)
    return (
      <Box flexDirection="column">
        <Box flexDirection="row">
          {tiles.map(([k, v]) => (
            <Box flexDirection="column" marginRight={2}>
              <Text dimColor>{k}</Text>
              <Text bold>{v}</Text>
            </Box>
          ))}
        </Box>
        <Text> </Text>
        <Text bold>LOGBOOK</Text>
        <Text dimColor>strength {level}/6 · {s.confirmed} of {s.active} signed off by you{trend ? ` · airworthiness ${trend}` : ''}</Text>
        {aircraftRows(s).map(row => <Text color={hullColor}>{row}</Text>)}
        {release && <Text color={hullColor} bold>{release}</Text>}
        <Text> </Text>
        <Text bold>CREW</Text>
        <Box flexDirection="row">
          {opus && (
            <Box flexDirection="column" marginRight={3}>
              <Text color="magenta">  ┌─────┐  </Text>
              <Text color="magenta">  │ ◉ ◉ │  CAPT {short(opus.model)}</Text>
              <Text color="magenta">  │  ‿  │  advisor · {opus.provider}</Text>
              <Text color="magenta">  └─┬─┬─┘  </Text>
            </Box>
          )}
          {haiku && (
            <Box flexDirection="column" marginRight={3}>
              <Text color="blue">  [◦◦]  F/O {short(haiku.model)}</Text>
              <Text color="blue">   ╵╵   extractor · {haiku.provider}</Text>
            </Box>
          )}
          {jev && (
            <Box flexDirection="column" marginRight={3}>
              <Text color="cyan">   ◇   </Text>
              <Text color="cyan">  ◇◆◇  AVIONICS JEV</Text>
              <Text color="cyan">   ◇   decision model</Text>
            </Box>
          )}
          {emb && (
            <Box flexDirection="column">
              <Text color="green">  ⊕  RADAR embeddings</Text>
              <Text color="green">     {emb.provider}</Text>
            </Box>
          )}
          {!opus && !jev && !haiku && !emb && <Text dimColor>no crew reported yet (status.json appears at session start)</Text>}
        </Box>
        <Text> </Text>
        <Text bold>Flight log</Text>
        <Text dimColor>{cost.length ? cost.join('  |  ') : 'no model calls yet'}</Text>
        <Text bold>Squawk log</Text>
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
