/**
 * Dual-Agent Memory — OpenClaw Plugin
 *
 * Registers two subsystems:
 *   1. Context Engine ("dual-agent-memory") — archives full conversation
 *      history before compaction and reassembles it with epistemic metadata.
 *   2. Agent Tools — memory_search, memory_evaluate, memory_briefing,
 *      memory_status — that the agent can invoke at any time.
 *
 * The epistemic monitor runs as a lifecycle hook (before_prompt_build) that
 * evaluates every assistant turn against the archive, injecting contradiction
 * warnings and confidence signals into the system context.
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import path from "node:path";
import fs from "node:fs";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface MemoryChunk {
  chunk_id: string;
  session_id: string;
  turn_id: number;
  role: string;
  content: string;
  timestamp: string;
  topics: string[];
  entities: string[];
  decisions: string[];
  embedding: number[] | null;
}

interface SearchResult {
  chunk: MemoryChunk;
  score: number;
  match_type: "vector" | "keyword" | "hybrid";
}

interface ConfidenceSignal {
  score: number;
  flags: string[];
  citations: Citation[];
  recommendation: string;
}

interface Citation {
  current_statement: string;
  archived_statement: string;
  chunk_id: string;
  session_id: string;
  turn_id: number;
  confidence: number;
}

interface Interrupt {
  severity: "soft_nudge" | "hard_interrupt";
  message: string;
  suggested_query: string;
}

interface PluginConfig {
  dbPath?: string;
  embeddingDim?: number;
  softNudgeThreshold?: number;
  hardInterruptThreshold?: number;
  searchTopK?: number;
  maxChunkTokens?: number;
  keywordWeight?: number;
  vectorWeight?: number;
  maxObservedTurns?: number;
  autoEvaluate?: boolean;
  autoArchiveOnCompact?: boolean;
}

// ---------------------------------------------------------------------------
// Antonym pairs for contradiction detection
// ---------------------------------------------------------------------------

const ANTONYM_PAIRS: [string, string][] = [
  ["add", "remove"], ["enable", "disable"], ["use", "avoid"],
  ["include", "exclude"], ["allow", "deny"], ["start", "stop"],
  ["create", "delete"], ["accept", "reject"], ["open", "close"],
  ["show", "hide"], ["increase", "decrease"], ["upgrade", "downgrade"],
  ["connect", "disconnect"], ["install", "uninstall"],
  ["activate", "deactivate"], ["encrypt", "decrypt"],
  ["require", "optional"], ["sync", "async"],
  ["public", "private"], ["mutable", "immutable"],
];

const ANTONYM_MAP = new Map<string, Set<string>>();
for (const [a, b] of ANTONYM_PAIRS) {
  if (!ANTONYM_MAP.has(a)) ANTONYM_MAP.set(a, new Set());
  if (!ANTONYM_MAP.has(b)) ANTONYM_MAP.set(b, new Set());
  ANTONYM_MAP.get(a)!.add(b);
  ANTONYM_MAP.get(b)!.add(a);
}

const NEGATION_WORDS = new Set([
  "not", "no", "never", "don't", "doesn't", "shouldn't", "won't",
  "cannot", "can't", "isn't", "aren't", "wasn't", "weren't",
  "without", "neither", "nor", "avoid", "stop", "remove",
]);

const STOP_WORDS = new Set([
  "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
  "have", "has", "had", "do", "does", "did", "will", "would", "could",
  "should", "may", "might", "shall", "can", "need", "must", "i", "you",
  "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
  "my", "your", "his", "its", "our", "their", "this", "that", "these",
  "those", "what", "which", "who", "whom", "whose", "when", "where",
  "why", "how", "all", "each", "every", "both", "few", "more", "most",
  "other", "some", "such", "no", "not", "only", "own", "same", "so",
  "than", "too", "very", "just", "because", "as", "until", "while",
  "of", "at", "by", "for", "with", "about", "against", "between",
  "through", "during", "before", "after", "above", "below", "to",
  "from", "up", "down", "in", "out", "on", "off", "over", "under",
  "again", "further", "then", "once", "here", "there", "and", "but",
  "or", "if", "nor", "also", "however", "although", "though",
]);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function tokenize(text: string): string[] {
  return (text.toLowerCase().match(/[a-z0-9]+/g) ?? []).filter(
    (w) => w.length > 1 && !STOP_WORDS.has(w)
  );
}

/** Tokenize for contradiction detection — no stop-word filtering (matches Python). */
function tokenizeRaw(text: string): string[] {
  return text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
}

function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length !== b.length) return 0;
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  const denom = Math.sqrt(na) * Math.sqrt(nb);
  return denom < 1e-12 ? 0 : dot / denom;
}

/** Deterministic hash-based embedding (mirrors Python Embedder). */
function hashEmbed(text: string, dim: number): number[] {
  // Use a simple seeded hash to produce deterministic floats.
  // This mirrors the Python implementation's hash-based fallback.
  const raw: number[] = [];
  let block = 0;
  while (raw.length < dim) {
    const seed = `${block}:${text}`;
    let hash = 0;
    for (let i = 0; i < seed.length; i++) {
      hash = ((hash << 5) - hash + seed.charCodeAt(i)) | 0;
    }
    // Generate multiple values from one seed
    for (let j = 0; j < 8 && raw.length < dim; j++) {
      hash = ((hash * 1103515245 + 12345) & 0x7fffffff);
      raw.push(hash / 0x7fffffff * 2 - 1);
    }
    block++;
  }
  // Normalize
  const norm = Math.sqrt(raw.reduce((s, v) => s + v * v, 0));
  return norm < 1e-12 ? raw : raw.map((v) => v / norm);
}

function nowISO(): string {
  return new Date().toISOString();
}

function makeChunkIdGenerator(): () => string {
  let counter = 0;
  return () => `oc_${Date.now().toString(36)}_${(++counter).toString(36)}`;
}

// ---------------------------------------------------------------------------
// Context Archive (SQLite-backed memory store)
// ---------------------------------------------------------------------------

// Minimal interface for better-sqlite3 (avoids `any` on db property)
interface SqliteDb {
  pragma(source: string): unknown;
  exec(source: string): void;
  prepare(source: string): {
    run(...params: unknown[]): unknown;
    get(...params: unknown[]): any;
    all(...params: unknown[]): any[];
  };
  transaction<T>(fn: () => T): () => T;
  close(): void;
}

// Use createRequire for ESM compatibility with better-sqlite3 (native addon)
import { createRequire } from "node:module";
const require_ = createRequire(import.meta.url);

class ContextArchive {
  private db: SqliteDb;
  private dim: number;

  constructor(dbPath: string, dim: number) {
    this.dim = dim;
    const Database = require_("better-sqlite3");
    this.db = new Database(dbPath) as SqliteDb;
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("foreign_keys = ON");
    this._createTables();
  }

  private _createTables(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS chunks (
        chunk_id   TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        turn_id    INTEGER NOT NULL,
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        timestamp  TEXT NOT NULL,
        topics     TEXT NOT NULL DEFAULT '[]',
        entities   TEXT NOT NULL DEFAULT '[]',
        decisions  TEXT NOT NULL DEFAULT '[]',
        embedding  BLOB
      )
    `);
    this.db.exec(`
      CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id)
    `);
    this.db.exec(`
      CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts
      USING fts5(chunk_id, content)
    `);
  }

  storeChunk(chunk: MemoryChunk): void {
    const embBlob = chunk.embedding
      ? Buffer.from(new Float32Array(chunk.embedding).buffer)
      : null;

    this.db.prepare(`
      INSERT OR REPLACE INTO chunks
      (chunk_id, session_id, turn_id, role, content, timestamp, topics, entities, decisions, embedding)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      chunk.chunk_id, chunk.session_id, chunk.turn_id, chunk.role,
      chunk.content, chunk.timestamp,
      JSON.stringify(chunk.topics), JSON.stringify(chunk.entities),
      JSON.stringify(chunk.decisions), embBlob,
    );

    // FTS
    this.db.prepare("DELETE FROM chunk_fts WHERE chunk_id = ?").run(chunk.chunk_id);
    this.db.prepare("INSERT INTO chunk_fts (chunk_id, content) VALUES (?, ?)").run(
      chunk.chunk_id, chunk.content
    );
  }

  storeChunks(chunks: MemoryChunk[]): void {
    const tx = this.db.transaction(() => {
      for (const c of chunks) this.storeChunk(c);
    });
    tx();
  }

  getChunk(chunkId: string): MemoryChunk | null {
    const row = this.db.prepare(
      "SELECT * FROM chunks WHERE chunk_id = ?"
    ).get(chunkId);
    return row ? this._rowToChunk(row) : null;
  }

  getAllChunks(): MemoryChunk[] {
    return this.db.prepare(
      "SELECT * FROM chunks ORDER BY turn_id"
    ).all().map((r: any) => this._rowToChunk(r));
  }

  getAllDecisions(): MemoryChunk[] {
    return this.db.prepare(
      "SELECT * FROM chunks WHERE decisions != '[]' ORDER BY timestamp"
    ).all().map((r: any) => this._rowToChunk(r));
  }

  getSessionChunks(sessionId: string): MemoryChunk[] {
    return this.db.prepare(
      "SELECT * FROM chunks WHERE session_id = ? ORDER BY turn_id"
    ).all(sessionId).map((r: any) => this._rowToChunk(r));
  }

  countChunks(): number {
    return this.db.prepare("SELECT COUNT(*) as c FROM chunks").get().c;
  }

  close(): void {
    this.db.close();
  }

  private _rowToChunk(row: any): MemoryChunk {
    let embedding: number[] | null = null;
    if (row.embedding) {
      const buf = row.embedding;
      const fa = new Float32Array(buf.buffer, buf.byteOffset, buf.byteLength / 4);
      embedding = Array.from(fa);
    }
    return {
      chunk_id: row.chunk_id,
      session_id: row.session_id,
      turn_id: row.turn_id,
      role: row.role,
      content: row.content,
      timestamp: row.timestamp,
      topics: JSON.parse(row.topics),
      entities: JSON.parse(row.entities),
      decisions: JSON.parse(row.decisions),
      embedding,
    };
  }
}

// ---------------------------------------------------------------------------
// Chunker — extracts topics, entities, decisions from messages
// ---------------------------------------------------------------------------

const DECISION_RE = /[^.!?\n]*\b(decided|will use|going with|chose|selected|agreed|confirmed|let's use|we should|switching to|moving to|opting for|prefer|must use|need to use|plan to use|want to use)\b[^.!?\n]*[.!?\n]?/gi;
const IMPERATIVE_RE = /(?:^|\n)\s*([A-Z][a-z]+(?:\s+\S+){1,10}[.!])/g;
const TECH_TERM_RE = /\b([a-z]+[A-Z]\w+|[a-z]+_[a-z]+\w*|[A-Z]{2,}[A-Z_]*)\b/g;
const TOPIC_PREP_RE = /\b(?:about|regarding|for)\s+(?:the\s+|an?\s+)?([A-Z][A-Za-z0-9]*(?:[\s-]+[A-Za-z][A-Za-z0-9]*){0,3})/g;
const CAP_PHRASE_RE = /\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b/g;
const CAP_WORD_RE = /\b([A-Z][a-z]{2,})\b/g;
const MENTION_RE = /@(\w+)/g;
const QUOTED_RE = /"([^"]+)"/g;

function extractTopics(text: string): string[] {
  const seen = new Set<string>();
  const topics: string[] = [];
  for (const m of text.matchAll(TOPIC_PREP_RE)) {
    const p = m[1].trim();
    if (!seen.has(p.toLowerCase())) { seen.add(p.toLowerCase()); topics.push(p); }
  }
  for (const m of text.matchAll(CAP_PHRASE_RE)) {
    const p = m[1].trim();
    if (!seen.has(p.toLowerCase())) { seen.add(p.toLowerCase()); topics.push(p); }
  }
  return topics;
}

function extractEntities(text: string): string[] {
  const seen = new Set<string>();
  const entities: string[] = [];

  // Sentence starters to skip
  const starters = new Set<number>();
  const first = text.match(/^\s*(\w+)/);
  if (first && first.index !== undefined) starters.add(first.index + (first[0].length - first[1].length));
  for (const m of text.matchAll(/[.!?]\s+(\w)/g)) {
    if (m.index !== undefined) starters.add(m.index + m[0].length - 1);
  }

  for (const m of text.matchAll(CAP_WORD_RE)) {
    const w = m[1];
    if (m.index !== undefined && starters.has(m.index)) continue;
    const low = w.toLowerCase();
    if (!seen.has(low) && !STOP_WORDS.has(low)) { seen.add(low); entities.push(w); }
  }
  for (const m of text.matchAll(MENTION_RE)) {
    const mention = `@${m[1]}`;
    if (!seen.has(mention.toLowerCase())) { seen.add(mention.toLowerCase()); entities.push(mention); }
  }
  for (const m of text.matchAll(QUOTED_RE)) {
    const t = m[1].trim();
    if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); entities.push(t); }
  }
  // Technical terms (camelCase, snake_case, ALL_CAPS)
  for (const m of text.matchAll(TECH_TERM_RE)) {
    const t = m[1];
    const low = t.toLowerCase();
    if (!seen.has(low) && !STOP_WORDS.has(low)) { seen.add(low); entities.push(t); }
  }
  return entities;
}

function extractDecisions(text: string): string[] {
  const seen = new Set<string>();
  const decisions: string[] = [];
  for (const m of text.matchAll(DECISION_RE)) {
    let s = m[0].trim().replace(/^\s*(?:and|but|so)\s+/i, "").trim();
    if (s && !seen.has(s.toLowerCase())) { seen.add(s.toLowerCase()); decisions.push(s); }
  }
  return decisions;
}

function chunkMessage(
  role: string, content: string, sessionId: string, turnId: number,
  idGen: () => string,
): MemoryChunk {
  return {
    chunk_id: idGen(),
    session_id: sessionId,
    turn_id: turnId,
    role,
    content,
    timestamp: nowISO(),
    topics: extractTopics(content),
    entities: extractEntities(content),
    decisions: extractDecisions(content),
    embedding: null,
  };
}

// ---------------------------------------------------------------------------
// Hybrid Search (vector + keyword with RRF)
// ---------------------------------------------------------------------------

const RRF_K = 60;

function hybridSearch(
  archive: ContextArchive,
  query: string,
  dim: number,
  topK: number,
  vectorWeight: number,
  keywordWeight: number,
): SearchResult[] {
  const chunks = archive.getAllChunks();
  if (chunks.length === 0) return [];

  const queryEmb = hashEmbed(query, dim);
  const queryTerms = tokenize(query);

  // Vector ranking
  const vectorRanked: { chunk: MemoryChunk; sim: number }[] = [];
  for (const c of chunks) {
    if (!c.embedding) continue;
    vectorRanked.push({ chunk: c, sim: cosineSimilarity(queryEmb, c.embedding) });
  }
  vectorRanked.sort((a, b) => b.sim - a.sim);

  // Keyword ranking
  const keywordRanked: { chunk: MemoryChunk; score: number }[] = [];
  if (queryTerms.length > 0) {
    for (const c of chunks) {
      const cl = c.content.toLowerCase();
      const meta = [...c.topics, ...c.entities].join(" ").toLowerCase();
      let hits = 0;
      for (const t of queryTerms) {
        if (cl.includes(t)) hits++;
        if (meta.includes(t)) hits += 0.5;
      }
      if (hits > 0) keywordRanked.push({ chunk: c, score: hits / queryTerms.length });
    }
    keywordRanked.sort((a, b) => b.score - a.score);
  }

  // RRF fusion
  const vRank = new Map<string, number>();
  vectorRanked.forEach((v, i) => vRank.set(v.chunk.chunk_id, i + 1));
  const kRank = new Map<string, number>();
  keywordRanked.forEach((v, i) => kRank.set(v.chunk.chunk_id, i + 1));

  const chunkMap = new Map<string, MemoryChunk>();
  for (const v of vectorRanked) chunkMap.set(v.chunk.chunk_id, v.chunk);
  for (const v of keywordRanked) chunkMap.set(v.chunk.chunk_id, v.chunk);

  const fused: { id: string; score: number; type: "vector" | "keyword" | "hybrid" }[] = [];
  for (const [id] of chunkMap) {
    const vr = vRank.get(id);
    const kr = kRank.get(id);
    let score = 0;
    if (vr !== undefined) score += vectorWeight * (1 / (vr + RRF_K));
    if (kr !== undefined) score += keywordWeight * (1 / (kr + RRF_K));
    const type = vr !== undefined && kr !== undefined ? "hybrid" : vr !== undefined ? "vector" : "keyword";
    fused.push({ id, score, type });
  }
  fused.sort((a, b) => b.score - a.score);

  return fused.slice(0, topK).map((f) => ({
    chunk: chunkMap.get(f.id)!,
    score: f.score,
    match_type: f.type,
  }));
}

// ---------------------------------------------------------------------------
// Contradiction Detector
// ---------------------------------------------------------------------------

function detectContradictions(
  currentText: string,
  archivedDecisions: MemoryChunk[],
): { current_statement: string; archived_statement: string; archived_chunk: MemoryChunk; confidence: number }[] {
  const seen = new Set<string>();
  const currentStatements: string[] = [];
  for (const m of currentText.matchAll(DECISION_RE)) {
    let s = m[0].trim().replace(/^\s*(?:and|but|so)\s+/i, "").trim();
    if (s && !seen.has(s.toLowerCase())) { seen.add(s.toLowerCase()); currentStatements.push(s); }
  }
  // Imperative sentences (e.g. "Use Redis for caching.")
  for (const m of currentText.matchAll(IMPERATIVE_RE)) {
    const s = m[1].trim();
    if (s && !seen.has(s.toLowerCase())) { seen.add(s.toLowerCase()); currentStatements.push(s); }
  }
  if (currentStatements.length === 0) return [];

  const contradictions: { current_statement: string; archived_statement: string; archived_chunk: MemoryChunk; confidence: number }[] = [];

  for (const stmt of currentStatements) {
    for (const chunk of archivedDecisions) {
      for (const archived of chunk.decisions) {
        const [isConflict, conf] = statementsConflict(stmt, archived);
        if (isConflict) {
          contradictions.push({
            current_statement: stmt,
            archived_statement: archived,
            archived_chunk: chunk,
            confidence: conf,
          });
        }
      }
    }
  }
  return contradictions;
}

function statementsConflict(s1: string, s2: string): [boolean, number] {
  const w1 = tokenizeRaw(s1);
  const w2 = tokenizeRaw(s2);
  if (!w1.length || !w2.length) return [false, 0];

  const set1 = new Set(w1);
  const set2 = new Set(w2);
  const overlap = new Set([...set1].filter((x) => set2.has(x)));
  const union = new Set([...set1, ...set2]);
  if (!union.size) return [false, 0];

  const overlapRatio = overlap.size / union.size;
  if (overlapRatio < 0.1) return [false, 0];

  const neg1 = w1.some((w) => NEGATION_WORDS.has(w));
  const neg2 = w2.some((w) => NEGATION_WORDS.has(w));
  const negConflict = neg1 !== neg2;

  let antonymScore = 0;
  for (const w of w1) {
    const ants = ANTONYM_MAP.get(w);
    if (ants && [...ants].some((a) => set2.has(a))) antonymScore++;
  }
  antonymScore = Math.min(1, antonymScore);

  // Choice conflict: "use X" vs "use Y"
  const choiceRe = /\b(use|chose|selected|going with|prefer)\s+(\S+)/gi;
  const m1 = [...s1.matchAll(choiceRe)];
  const m2 = [...s2.matchAll(choiceRe)];
  let choiceScore = 0;
  for (const a of m1) {
    for (const b of m2) {
      const o1 = a[2].toLowerCase().replace(/[.,!?]$/, "");
      const o2 = b[2].toLowerCase().replace(/[.,!?]$/, "");
      if (o1 !== o2) choiceScore = 1;
    }
  }

  let signal = 0;
  if (negConflict) signal += 0.5;
  signal += antonymScore * 0.4;
  signal += choiceScore * 0.4;

  const confidence = Math.min(1, signal * (0.5 + overlapRatio));
  return [confidence >= 0.3, Math.round(confidence * 1000) / 1000];
}

// ---------------------------------------------------------------------------
// Confidence Scorer (Epistemic Layer)
// ---------------------------------------------------------------------------

function scoreConfidence(
  outputText: string,
  archive: ContextArchive,
  dim: number,
  vectorWeight: number,
  keywordWeight: number,
  softThreshold: number,
  hardThreshold: number,
  currentContext?: string,
): ConfidenceSignal {
  // Extract entities/topics from output
  const entities = extractEntities(outputText);
  const topics = extractTopics(outputText);
  const gaps: string[] = [];
  const driftTopics: string[] = [];
  const contextLower = (currentContext ?? "").toLowerCase();

  // Check entities against archive — only flag as gap if entity IS in archive
  // but NOT in the current conversation context (matching Python behavior).
  for (const entity of entities) {
    const results = hybridSearch(archive, entity, dim, 3, vectorWeight, keywordWeight);
    if (results.length > 0 && contextLower && !contextLower.includes(entity.toLowerCase())) {
      gaps.push(entity);
    }
  }

  // Check topics — flag drift if topic has archive history but is absent from context
  for (const topic of topics) {
    const results = hybridSearch(archive, topic, dim, 3, vectorWeight, keywordWeight);
    if (results.length > 0 && contextLower && !contextLower.includes(topic.toLowerCase())) {
      driftTopics.push(topic);
    }
  }

  // Contradiction detection
  const archivedDecisions = archive.getAllDecisions();
  const contradictions = detectContradictions(outputText, archivedDecisions);

  // Compute score
  let score = 1.0;
  score -= 0.1 * gaps.length;
  score -= 0.2 * contradictions.length;
  score -= 0.05 * driftTopics.length;
  score = Math.max(0, Math.min(1, score));

  // Build flags
  const flags: string[] = [];
  if (gaps.length) flags.push("gap");
  if (contradictions.length) flags.push("contradiction");
  if (driftTopics.length) flags.push("drift");

  // Citations
  const citations: Citation[] = contradictions.map((c) => ({
    current_statement: c.current_statement,
    archived_statement: c.archived_statement,
    chunk_id: c.archived_chunk.chunk_id,
    session_id: c.archived_chunk.session_id,
    turn_id: c.archived_chunk.turn_id,
    confidence: c.confidence,
  }));

  // Recommendation
  let recommendation: string;
  if (flags.includes("contradiction")) {
    recommendation = "CONTRADICTION DETECTED: Output conflicts with prior archived decisions. Review citations and reconcile.";
  } else if (score < hardThreshold) {
    recommendation = "LOW CONFIDENCE: Significant gaps between output and archived knowledge. Retrieve relevant context.";
  } else if (score < softThreshold) {
    recommendation = "MODERATE CONFIDENCE: Some entities or topics lack context grounding. Consider verifying against archive.";
  } else {
    recommendation = "HIGH CONFIDENCE: Output is well-grounded in available context.";
  }

  return { score: Math.round(score * 1000) / 1000, flags, citations, recommendation };
}

// ---------------------------------------------------------------------------
// Plugin entry point
// ---------------------------------------------------------------------------

export default function dualAgentMemoryPlugin(api: OpenClawPluginApi) {
  const cfg: PluginConfig = (api as any).config ?? {};
  const dbPath = cfg.dbPath ?? `${process.env.HOME ?? "/tmp"}/.dual_agent_memory/memory.db`;
  const dim = cfg.embeddingDim ?? 384;
  const softThreshold = cfg.softNudgeThreshold ?? 0.7;
  const hardThreshold = cfg.hardInterruptThreshold ?? 0.5;
  const searchTopK = cfg.searchTopK ?? 5;
  const vectorWeight = cfg.vectorWeight ?? 0.7;
  const keywordWeight = cfg.keywordWeight ?? 0.3;
  const maxObservedTurns = cfg.maxObservedTurns ?? 50;
  const autoEvaluate = cfg.autoEvaluate ?? true;
  const autoArchive = cfg.autoArchiveOnCompact ?? true;

  // Ensure DB directory exists (skip for in-memory databases)
  if (dbPath !== ":memory:") {
    const dir = path.dirname(dbPath);
    fs.mkdirSync(dir, { recursive: true });
  }

  const archive = new ContextArchive(dbPath, dim);
  const newChunkId = makeChunkIdGenerator();
  let sessionId = `session-${Date.now()}`;
  const observedTurns: { role: string; content: string }[] = [];
  let rollingSummary = "";

  api.logger.info(`[dual-agent-memory] Initialized with db=${dbPath}, dim=${dim}`);

  // -----------------------------------------------------------------
  // 1. CONTEXT ENGINE — manages archival and context assembly
  // -----------------------------------------------------------------
  api.registerContextEngine("dual-agent-memory", () => ({
    info: { id: "dual-agent-memory", name: "Dual Agent Memory", ownsCompaction: true },

    async bootstrap(_params: { sessionId: string; sessionKey?: string; sessionFile: string }) {
      api.logger.info(
        `[dual-agent-memory] Context engine bootstrapped. Archive has ${archive.countChunks()} chunks.`
      );
      return { bootstrapped: true };
    },

    async ingest(params: { sessionId: string; sessionKey?: string; message: any; isHeartbeat?: boolean }) {
      const message = params.message;
      // Record every message in the observation buffer
      observedTurns.push({ role: message.role, content: message.content ?? "" });

      // Self-compact if buffer grows too large
      if (observedTurns.length > maxObservedTurns) {
        const mid = Math.floor(observedTurns.length / 2);
        const older = observedTurns.splice(0, mid);
        const compactionSession = `compaction-${Date.now().toString(36)}`;
        const chunks = older.map((m, i) => {
          const c = chunkMessage(m.role, m.content, compactionSession, i + 1, newChunkId);
          c.embedding = hashEmbed(c.content, dim);
          return c;
        });
        archive.storeChunks(chunks);
        rollingSummary += `\nCompacted ${older.length} turns into ${chunks.length} chunks.`;
        api.logger.info(`[dual-agent-memory] Self-compacted ${older.length} turns.`);
      }
      return { ingested: true };
    },

    async assemble(params: { sessionId: string; sessionKey?: string; messages: any[]; tokenBudget?: number }) {
      const { messages } = params;
      // On assembly, inject a briefing of relevant archived context
      const recent = observedTurns.slice(-5);
      const recentText = recent.map((t) => t.content).join(" ");
      const relevant = hybridSearch(archive, recentText, dim, 3, vectorWeight, keywordWeight);

      if (relevant.length > 0) {
        const contextBlock = relevant.map((r) =>
          `[Archive: ${r.chunk.session_id} T${r.chunk.turn_id}] ${r.chunk.content}`
        ).join("\n");

        // Prepend archived context as a system message
        const archiveContent = `[Dual-Agent Memory — Archived Context]\n${contextBlock}`;
        const archiveMsg = { role: "system", content: archiveContent } as any;
        const estimatedTokens = Math.ceil(archiveContent.length / 4);
        return { messages: [archiveMsg, ...messages], estimatedTokens };
      }

      return { messages, estimatedTokens: 0 };
    },

    async compact(_params: { sessionId: string; sessionKey?: string; sessionFile: string }) {
      // Archive all observed turns before compaction
      if (!autoArchive || observedTurns.length === 0) {
        return { ok: true, compacted: false, reason: "nothing to compact" };
      }

      const chunks = observedTurns.map((m, i) => {
        const c = chunkMessage(m.role, m.content, sessionId, i + 1, newChunkId);
        c.embedding = hashEmbed(c.content, dim);
        return c;
      });
      archive.storeChunks(chunks);
      api.logger.info(
        `[dual-agent-memory] Archived ${chunks.length} chunks before compaction.`
      );
      return { ok: true, compacted: true };
    },

    async prepareSubagentSpawn(_params: { parentSessionKey: string; childSessionKey: string; ttlMs?: number }) {
      // Give subagents a snapshot of recent decisions
      const decisions = archive.getAllDecisions().slice(-10);
      if (decisions.length > 0) {
        api.logger.info(
          `[dual-agent-memory] Prior decisions: ${decisions.flatMap((d) => d.decisions).join("; ")}`
        );
      }
      return { rollback: () => {} };
    },

    async onSubagentEnded(_params: { childSessionKey: string; reason: string }) {
      // Subagent ended — no-op for now (we archive via ingest)
    },
  }));

  // -----------------------------------------------------------------
  // 2. EPISTEMIC HOOK — evaluates assistant turns automatically
  // -----------------------------------------------------------------
  if (autoEvaluate) {
    api.registerHook(
      "agent",
      async (_event) => {
        // Find the latest assistant message in observed turns
        const lastAssistant = [...observedTurns].reverse().find((t) => t.role === "assistant");
        if (!lastAssistant) return;

        // Build context summary from recent observed turns
        const contextSummary = observedTurns.slice(-10).map((t) => t.content).join("\n");
        const signal = scoreConfidence(
          lastAssistant.content, archive, dim,
          vectorWeight, keywordWeight, softThreshold, hardThreshold,
          contextSummary,
        );

        if (signal.flags.length === 0) return;

        // Log epistemic warning
        const parts: string[] = [`[Epistemic Monitor] Confidence: ${signal.score}`];
        if (signal.flags.length) parts.push(`Flags: ${signal.flags.join(", ")}`);
        parts.push(signal.recommendation);

        for (const c of signal.citations) {
          parts.push(
            `  Contradiction: "${c.current_statement}" vs archived "${c.archived_statement}" ` +
            `(${c.session_id} T${c.turn_id}, confidence=${c.confidence})`
          );
        }

        api.logger.warn(parts.join("\n"));
      },
      { name: "dual-agent-memory.epistemic-monitor" }
    );
  }

  // -----------------------------------------------------------------
  // 3. AGENT TOOLS — searchable by the agent
  // -----------------------------------------------------------------

  api.registerTool({
    name: "memory_search",
    description:
      "Search the conversation memory archive for relevant context from previous sessions. " +
      "Use when you need to recall past decisions, verify consistency, or retrieve historical context.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "What to search for in the memory archive" },
        top_k: { type: "integer", description: "Max results (default 5)", default: 5 },
        session_id: { type: "string", description: "Optional: limit to a specific session" },
      },
      required: ["query"],
    },
    execute: async (_toolCallId: string, params: any) => {
      const { query, top_k, session_id } = params;
      const k = top_k ?? searchTopK;
      let results = hybridSearch(archive, query, dim, k, vectorWeight, keywordWeight);
      if (session_id) {
        results = results.filter((r) => r.chunk.session_id === session_id);
      }
      return {
        content: JSON.stringify({
          query,
          results: results.map((r) => ({
            content: r.chunk.content,
            session_id: r.chunk.session_id,
            turn_id: r.chunk.turn_id,
            role: r.chunk.role,
            topics: r.chunk.topics,
            entities: r.chunk.entities,
            decisions: r.chunk.decisions,
            timestamp: r.chunk.timestamp,
            relevance_score: r.score,
            match_type: r.match_type,
          })),
          total_results: results.length,
        }),
      };
    },
  } as any);

  api.registerTool({
    name: "memory_evaluate",
    description:
      "Evaluate an assistant output against the memory archive. Returns a confidence score (0-1) " +
      "and any contradiction, gap, or drift flags. Use before finalizing important decisions.",
    parameters: {
      type: "object",
      properties: {
        output_text: { type: "string", description: "The assistant output to evaluate" },
      },
      required: ["output_text"],
    },
    execute: async (_toolCallId: string, params: any) => {
      const { output_text } = params;
      const contextSummary = observedTurns.slice(-10).map((t) => t.content).join("\n");
      const signal = scoreConfidence(
        output_text, archive, dim, vectorWeight, keywordWeight,
        softThreshold, hardThreshold, contextSummary,
      );

      const result: any = {
        score: signal.score,
        flags: signal.flags,
        recommendation: signal.recommendation,
        citations: signal.citations,
      };

      // Check for interrupt
      if (signal.flags.includes("contradiction")) {
        result.interrupt = {
          severity: "hard_interrupt",
          message: `CONTRADICTION: ${signal.citations.map((c: any) =>
            `"${c.current_statement}" vs "${c.archived_statement}"`
          ).join("; ")}`,
          suggested_query: signal.citations[0]?.current_statement ?? "recent decisions",
        };
      } else if (signal.score < hardThreshold) {
        result.interrupt = {
          severity: "hard_interrupt",
          message: `LOW CONFIDENCE (${signal.score}): ${signal.recommendation}`,
          suggested_query: "recent decisions",
        };
      } else if (signal.score < softThreshold) {
        result.interrupt = {
          severity: "soft_nudge",
          message: `MODERATE CONFIDENCE (${signal.score}): ${signal.recommendation}`,
          suggested_query: "recent context",
        };
      }

      return { content: JSON.stringify(result) };
    },
  } as any);

  api.registerTool({
    name: "memory_briefing",
    description:
      "Get a session-start briefing with prior decisions, active topics, and entity state. " +
      "Call at the beginning of a new session.",
    parameters: {
      type: "object",
      properties: {
        session_id: { type: "string", description: "Current session identifier" },
      },
      required: ["session_id"],
    },
    execute: async (_toolCallId: string, params: any) => {
      const sid = params.session_id;
      const decisionChunks = archive.getAllDecisions().slice(-20);
      const allTopics = new Map<string, string>();
      const allEntities = new Map<string, string>();

      for (const c of decisionChunks) {
        for (const t of c.topics) allTopics.set(t, c.session_id);
        for (const e of c.entities) allEntities.set(e, `Last: ${c.session_id} T${c.turn_id}`);
      }

      const lines = ["=== Session Briefing ===", "Prior Decisions:"];
      if (decisionChunks.length) {
        for (const c of decisionChunks) {
          for (const d of c.decisions) {
            lines.push(`- ${d} (Session ${c.session_id}, Turn ${c.turn_id})`);
          }
        }
      } else {
        lines.push("- (none)");
      }
      lines.push(`Active Topics: ${allTopics.size ? [...allTopics.keys()].join(", ") : "(none)"}`);
      lines.push("Key Entities:");
      if (allEntities.size) {
        for (const [e, s] of allEntities) lines.push(`- ${e}: ${s}`);
      } else {
        lines.push("- (none)");
      }
      lines.push("===");

      return { content: JSON.stringify({ session_id: sid, briefing: lines.join("\n"), archive_size: archive.countChunks() }) };
    },
  } as any);

  api.registerTool({
    name: "memory_archive",
    description:
      "Archive conversation messages to the memory store. Use before context compaction " +
      "or at session end to preserve conversation history.",
    parameters: {
      type: "object",
      properties: {
        messages: {
          type: "array",
          items: {
            type: "object",
            properties: {
              role: { type: "string" },
              content: { type: "string" },
            },
            required: ["role", "content"],
          },
          description: "Messages to archive",
        },
        session_id: { type: "string", description: "Session identifier" },
      },
      required: ["messages", "session_id"],
    },
    execute: async (_toolCallId: string, params: any) => {
      const { messages, session_id: sid } = params;
      const before = archive.countChunks();
      const chunks = messages.map((m: any, i: number) => {
        const c = chunkMessage(m.role, m.content, sid, i + 1, newChunkId);
        c.embedding = hashEmbed(c.content, dim);
        return c;
      });
      archive.storeChunks(chunks);
      return {
        content: JSON.stringify({
          messages_captured: messages.length,
          chunks_created: chunks.length,
          chunks_before: before,
          chunks_after: archive.countChunks(),
          session_id: sid,
        }),
      };
    },
  } as any);

  api.registerTool({
    name: "memory_status",
    description: "Get the current status of the memory system including archive size and monitor state.",
    parameters: { type: "object", properties: {} },
    execute: async () => ({
      content: JSON.stringify({
        archive_chunks: archive.countChunks(),
        observed_turns: observedTurns.length,
        has_rolling_summary: rollingSummary.length > 0,
        session_id: sessionId,
        config: {
          dim, softThreshold, hardThreshold, searchTopK,
          vectorWeight, keywordWeight, maxObservedTurns,
          autoEvaluate, autoArchive,
        },
      }),
    }),
  } as any);

  // -----------------------------------------------------------------
  // 4. CLI COMMAND — `openclaw memory <subcommand>`
  // -----------------------------------------------------------------
  api.registerCli(({ program }: any) => {
    const mem = program.command("memory").description("Dual-agent memory management");

    mem.command("status")
      .description("Show memory archive status")
      .action(() => {
        console.log(`Archive: ${archive.countChunks()} chunks`);
        console.log(`Session: ${sessionId}`);
        console.log(`Observed turns: ${observedTurns.length}`);
        console.log(`DB: ${dbPath}`);
      });

    mem.command("search")
      .description("Search the memory archive")
      .argument("<query>", "Search query")
      .option("-k, --top-k <n>", "Max results", "5")
      .action((query: string, opts: any) => {
        const results = hybridSearch(archive, query, dim, parseInt(opts.topK), vectorWeight, keywordWeight);
        if (results.length === 0) {
          console.log("No results found.");
          return;
        }
        for (const r of results) {
          console.log(`\n--- [${r.match_type}] score=${r.score.toFixed(4)} ---`);
          console.log(`Session: ${r.chunk.session_id} | Turn: ${r.chunk.turn_id} | Role: ${r.chunk.role}`);
          console.log(r.chunk.content.slice(0, 200));
          if (r.chunk.decisions.length) console.log(`Decisions: ${r.chunk.decisions.join("; ")}`);
        }
      });

    mem.command("decisions")
      .description("List all archived decisions")
      .action(() => {
        const decisions = archive.getAllDecisions();
        if (decisions.length === 0) {
          console.log("No decisions archived yet.");
          return;
        }
        for (const c of decisions) {
          for (const d of c.decisions) {
            console.log(`[${c.session_id} T${c.turn_id}] ${d}`);
          }
        }
      });
  });

  // -----------------------------------------------------------------
  // 5. BACKGROUND SERVICE — periodic consolidation
  // -----------------------------------------------------------------
  let consolidationTimer: ReturnType<typeof setInterval> | null = null;

  api.registerService({
    id: "dual-agent-memory-consolidator",
    start: async () => {
      api.logger.info("[dual-agent-memory] Consolidation service started.");
      // Run consolidation every 5 minutes
      consolidationTimer = setInterval(() => {
        const decisionChunks = archive.getAllDecisions();
        if (decisionChunks.length < 2) return;

        // Group by topic, supersede older decisions
        const topicGroups = new Map<string, MemoryChunk[]>();
        for (const c of decisionChunks) {
          for (const t of c.topics) {
            const key = t.toLowerCase();
            if (!topicGroups.has(key)) topicGroups.set(key, []);
            topicGroups.get(key)!.push(c);
          }
        }

        let superseded = 0;
        for (const [, group] of topicGroups) {
          if (group.length < 2) continue;
          group.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
          const newest = group[group.length - 1];
          for (const older of group.slice(0, -1)) {
            if (older.content.startsWith("[SUPERSEDED")) continue;
            older.content = `[SUPERSEDED by ${newest.chunk_id}] ${older.content}`;
            older.embedding = hashEmbed(older.content, dim);
            archive.storeChunk(older);
            superseded++;
          }
        }

        if (superseded > 0) {
          api.logger.info(`[dual-agent-memory] Consolidated: ${superseded} decisions superseded.`);
        }
      }, 5 * 60 * 1000);
    },
    stop: async () => {
      if (consolidationTimer) clearInterval(consolidationTimer);
      archive.close();
      api.logger.info("[dual-agent-memory] Consolidation service stopped.");
    },
  });
}
