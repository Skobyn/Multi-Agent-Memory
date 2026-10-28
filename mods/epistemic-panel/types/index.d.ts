export type Intervention = { ts: string; kind: string; detail: string; isDeny: boolean }

export type Component = {
  role: 'decision_model' | 'advisor' | 'extractor' | 'embeddings'
  provider: string
  model: string
  isPresent: boolean          // configured and not reported unavailable this session
  calls: number
  ms: number
  tokens: number
}

export type Snapshot = {
  dataDir: string
  isPresent: boolean
  active: number
  confirmed: number
  review: number
  retention: number | null
  retentions: number[]        // one per compaction audit this session, oldest first
  lastAudit: string
  pinnedChars: number
  denies: number
  blocks: number
  denyBudget: number
  components: Component[]
  recent: Intervention[]
  readAt: number
}

declare module 'claude-code' {
  interface PluginState {
    'epistemic-panel': { snapshot: Shaped<Snapshot | null> }
  }
}
