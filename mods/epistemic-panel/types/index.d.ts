export type Intervention = { ts: string; kind: string; detail: string; isDeny: boolean }

export type Snapshot = {
  dataDir: string
  isPresent: boolean
  active: number
  review: number
  retention: number | null
  lastAudit: string
  pinnedChars: number
  denies: number
  blocks: number
  recent: Intervention[]
  readAt: number
}

declare module 'claude-code' {
  interface PluginState {
    'epistemic-panel': { snapshot: Shaped<Snapshot | null> }
  }
}
