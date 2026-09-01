#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import * as lancedb from '@lancedb/lancedb';
import { loadConfig, buildChunks } from './sources.js';
import { getQwenEmbedder } from './embed-qwen.js';
import { loadEnrichmentCache, applyAuxiliaryEnrichment, validateEnrichmentJsonl } from './enrichment.js';
import { evaluateBenchmark, benchmarkPasses } from './benchmark.js';

function arg(name, fallback = undefined) {
  const idx = process.argv.indexOf(`--${name}`);
  if (idx >= 0 && idx + 1 < process.argv.length) return process.argv[idx + 1];
  return fallback;
}
function flag(name) { return process.argv.includes(`--${name}`); }
function ensureDir(p) { fs.mkdirSync(p, { recursive: true }); }
function nowStamp() { return new Date().toISOString().replace(/[:.]/g, '-'); }

const RAW_APPROVAL_STATES = new Set(['NOT_CONFIRMED', 'APPROVED_EXTERNAL', 'LOCAL_ONLY']);
const EXACT_VALIDATION_STATES = new Set(['REQUIRED', 'SKIPPED_PRIVACY_GATE']);

export function privacyGate(config) {
  const raw = config.privacy?.discordRawApproval || 'NOT_CONFIRMED';
  const exact = config.privacy?.exactMessageIdValidation || 'SKIPPED_PRIVACY_GATE';
  if (!RAW_APPROVAL_STATES.has(raw)) throw new Error(`Invalid privacy.discordRawApproval: ${raw}`);
  if (!EXACT_VALIDATION_STATES.has(exact)) throw new Error(`Invalid privacy.exactMessageIdValidation: ${exact}`);
  const rawConfigured = (config.sources || []).some((source) => source.sourceType === 'discord_raw');
  if (rawConfigured && raw === 'NOT_CONFIRMED') throw new Error('discord_raw source requires APPROVED_EXTERNAL or LOCAL_ONLY');
  if (!rawConfigured && exact !== 'SKIPPED_PRIVACY_GATE') throw new Error('exact message lookup must be SKIPPED_PRIVACY_GATE without discord_raw');
  return { discordRawApproval: raw, exactMessageIdValidation: exact };
}

const INDEX_SCHEMA_VERSION = 2;

function embeddingIdentity(embedding = {}) {
  return {
    provider: embedding.provider ?? null,
    model: embedding.model ?? null,
    dimensions: embedding.dimensions ?? null,
    documentTaskType: embedding.documentTaskType ?? null,
    nativeDimensions: embedding.nativeDimensions ?? null,
    quantization: embedding.quantization ?? null,
    modelSha256: embedding.modelSha256 ?? null,
    runtimeRevision: embedding.runtimeRevision ?? null,
    pooling: embedding.pooling ?? null,
    queryInstruction: embedding.queryInstruction ?? null,
    normalization: embedding.normalization ?? null
  };
}

function enrichmentState(config) {
  const loaded = loadEnrichmentCache(config.enrichment || {});
  return {
    enabled: loaded.enabled,
    inputPath: loaded.inputPath || null,
    inputSha256: loaded.sha256 || null,
    minConfidence: config.enrichment?.minConfidence ?? 0.75,
    valid: loaded.stats.valid,
    invalid: loaded.stats.invalid
  };
}

function buildFingerprint(config) {
  const semantic = {
    schemaVersion: INDEX_SCHEMA_VERSION,
    embedding: embeddingIdentity(config.embedding),
    chunking: config.chunking || {},
    enrichment: enrichmentState(config)
  };
  return crypto.createHash('sha256').update(JSON.stringify(semantic)).digest('hex');
}

function statePath() { return path.resolve('data/index-state.json'); }
function loadIndexState() {
  const p = statePath();
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}
function writeIndexState(config, built, extra = {}, { merge = false } = {}) {
  ensureDir(path.dirname(statePath()));
  const files = {};
  for (const c of built.chunks) {
    if (!files[c.source_path]) {
      files[c.source_path] = {
        source_path: c.source_path,
        rel_path: c.rel_path,
        source_id: c.source_id,
        source_type: c.source_type,
        project: c.project,
        channel: c.channel,
        file_sha256: c.file_sha256,
        file_mtime_ms: c.file_mtime_ms,
        file_bytes: c.file_bytes,
        chunk_ids: [],
        chunks: 0
      };
    }
    files[c.source_path].chunk_ids.push(c.id);
    files[c.source_path].chunks += 1;
  }
  // Merge mode (partial index): read the previous state and only replace the paths touched by
  // this run instead of rewriting the whole file map.
  let mergedFiles = files;
  if (merge) {
    const prev = loadIndexState();
    if (prev?.files) mergedFiles = { ...prev.files, ...files };
  }
  const state = {
    version: INDEX_SCHEMA_VERSION,
    updatedAt: new Date().toISOString(),
    tableName: config.tableName || 'knowledge_chunks',
    dbPath: path.resolve(config.dbPath || './data/lancedb'),
    embedding: config.embedding,
    chunking: config.chunking,
    enrichment: enrichmentState(config),
    buildFingerprint: buildFingerprint(config),
    // Always derive docs from the state file map: with a partial index + overwrite, built.docs
    // counts the whole corpus and would contradict the filtered files/chunks in the same state.
    docs: Object.keys(mergedFiles).length,
    chunks: merge ? Object.values(mergedFiles).reduce((a, f) => a + (f.chunks || 0), 0) : built.chunks.length,
    files: mergedFiles,
    ...extra
  };
  fs.writeFileSync(statePath(), JSON.stringify(state, null, 2));
  return state;
}
function groupChunksByPath(chunks) {
  const map = new Map();
  for (const c of chunks) {
    if (!map.has(c.source_path)) map.set(c.source_path, []);
    map.get(c.source_path).push(c);
  }
  return map;
}
function sqlString(s) { return String(s).replaceAll("'", "''"); }
async function deleteSourcePaths(table, paths) {
  const list = [...paths];
  const chunkSize = 25;
  for (let i = 0; i < list.length; i += chunkSize) {
    const pred = list.slice(i, i + chunkSize).map((p) => `source_path = '${sqlString(p)}'`).join(' OR ');
    if (pred) await table.delete(pred);
  }
}
// Single source of truth for embedding input text; compact-cache derives cache keys with the
// same function, so the two must stay in sync.
function chunkEmbedText(c) {
  return `${c.project}\n${c.title}\n${c.heading}\n${c.chunk_text}`;
}

async function rowsForChunks(config, chunks) {
  const enrichment = loadEnrichmentCache(config.enrichment || {});
  const prepared = chunks.map((chunk) => applyAuxiliaryEnrichment(
    chunk,
    enrichment.records.get(chunk.id) || null,
    { enabled: enrichment.enabled }
  ));
  const dims = config.embedding?.dimensions || 768;
  const embeddingProvider = config.embedding?.provider || 'qwen-local';
  if (embeddingProvider !== 'qwen-local') throw new Error(`Only qwen-local is supported; got ${embeddingProvider}`);
  const vectors = await getQwenEmbedder(config.embedding).embedDocuments(
    prepared.map((c) => chunkEmbedText(c)),
    (progress) => console.error(`[embedding] local embedded ${progress.done}/${progress.total} (+${progress.batchSize})`)
  );
  return prepared.map((c, i) => ({
    ...c,
    embedding_provider: embeddingProvider,
    embedding_model: config.embedding?.model || embeddingProvider,
    embedding_dimensions: dims,
    vector: vectors[i]
  }));
}


async function openDb(config) {
  const dbPath = path.resolve(config.dbPath || './data/qwen-local-lancedb');
  ensureDir(dbPath);
  return lancedb.connect(dbPath);
}

async function commandScan(config) {
  const built = buildChunks(config);
  ensureDir('reports');
  const byProject = {};
  for (const c of built.chunks) byProject[c.project] = (byProject[c.project] || 0) + 1;
  const report = {
    generatedAt: new Date().toISOString(),
    embedding: config.embedding,
    docs: built.docs.length,
    chunks: built.chunks.length,
    skipped: built.skipped,
    secretHits: built.secretHits,
    byProject
  };
  fs.writeFileSync('reports/source-scan.latest.json', JSON.stringify(report, null, 2));
  fs.writeFileSync(`reports/source-scan.${nowStamp()}.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
}

async function commandIndex(config) {
  const limit = Number(arg('limit', '0')) || 0;
  const projectFilter = arg('project', '');
  const built = buildChunks(config);
  let chunks = built.chunks;
  if (projectFilter) chunks = chunks.filter((c) => c.project.toLowerCase() === projectFilter.toLowerCase());
  if (limit > 0) chunks = chunks.slice(0, limit);
  if (chunks.length === 0) throw new Error('No chunks to index');

  const rows = await rowsForChunks(config, chunks);

  const db = await openDb(config);
  const tableName = config.tableName || 'knowledge_chunks';
  const mode = flag('append') ? 'append' : 'overwrite';
  if (mode === 'overwrite') {
    await db.createTable(tableName, rows, { mode: 'overwrite' });
  } else {
    let table = null;
    try { table = await db.openTable(tableName); } catch {}
    if (!table) {
      await db.createTable(tableName, rows, { mode: 'overwrite' });
    } else {
      // Delete existing rows for the same source_path before appending so re-running the same
      // batch of files does not accumulate duplicate chunks.
      await deleteSourcePaths(table, new Set(rows.map((r) => r.source_path)));
      await table.add(rows);
    }
  }

  ensureDir('reports');
  const manifest = {
    indexedAt: new Date().toISOString(),
    mode,
    tableName,
    dbPath: path.resolve(config.dbPath || './data/lancedb'),
    embedding: config.embedding,
    docs: built.docs.length,
    chunksIndexed: rows.length,
    chunksAvailable: built.chunks.length,
    projectFilter: projectFilter || null,
    limit: limit || null,
    skipped: built.skipped.length,
    secretHitFiles: built.secretHits.length
  };
  fs.writeFileSync('reports/index-manifest.latest.json', JSON.stringify(manifest, null, 2));
  fs.writeFileSync(`reports/index-manifest.${nowStamp()}.json`, JSON.stringify(manifest, null, 2));
  // A partial index (--project/--limit) with append merges the previous state (only the paths
  // touched this run); overwrite rebuilds the whole table, so the state is rewritten in full to
  // mirror the table contents.
  const partial = Boolean(projectFilter) || limit > 0;
  writeIndexState(config, { ...built, chunks }, { lastIndexMode: mode }, { merge: partial && mode === 'append' });
  console.log(JSON.stringify(manifest, null, 2));
}


async function validateSyncStateTable(config, table, built) {
  const schema = await table.schema();
  const fields = new Map(schema.fields.map((field) => [field.name, field]));
  const required = [
    'id', 'content_sha256', 'deterministic_doc_type', 'deterministic_tags_json',
    'deterministic_importance', 'deterministic_metadata_version', 'ai_enrichment_status',
    'ai_enrichment_schema_version', 'embedding_provider', 'embedding_model',
    'embedding_dimensions', 'vector'
  ];
  const missing = required.filter((name) => !fields.has(name));
  if (missing.length) throw new Error(`[sync-state] table is not schema v2 (missing: ${missing.join(', ')}); run a full index rebuild`);
  const vectorDimensions = Number(fields.get('vector')?.type?.listSize);
  if (vectorDimensions !== Number(config.embedding?.dimensions)) {
    throw new Error(`[sync-state] table vector dimensions ${vectorDimensions || 'unknown'} do not match configured ${config.embedding?.dimensions}; run a full index rebuild`);
  }

  const enrichment = loadEnrichmentCache(config.enrichment || {});
  const expected = new Map(built.chunks.map((chunk) => {
    const row = applyAuxiliaryEnrichment(chunk, enrichment.records.get(chunk.id), { enabled: enrichment.enabled });
    return [chunk.id, row];
  }));
  const selected = [
    'id', 'content_sha256', 'deterministic_doc_type', 'deterministic_tags_json',
    'deterministic_importance', 'deterministic_metadata_version', 'ai_doc_type',
    'ai_tags_json', 'ai_importance', 'ai_summary', 'ai_decisions_json', 'ai_risks_json',
    'ai_action_items_json', 'ai_confidence', 'ai_needs_review', 'ai_enrichment_status',
    'ai_enrichment_model', 'ai_enrichment_schema_version', 'embedding_provider',
    'embedding_model', 'embedding_dimensions'
  ];
  const rows = await table.query().select(selected).toArray();
  if (rows.length !== expected.size) {
    throw new Error(`[sync-state] table rows ${rows.length} do not match current chunks ${expected.size}; run a full index rebuild`);
  }
  const exactFields = selected.filter((name) => !['id', 'embedding_provider', 'embedding_model', 'embedding_dimensions'].includes(name));
  for (const row of rows) {
    const wanted = expected.get(row.id);
    if (!wanted) throw new Error(`[sync-state] table contains an unexpected chunk id; run a full index rebuild`);
    for (const field of exactFields) {
      if (row[field] !== wanted[field]) throw new Error(`[sync-state] table field ${field} does not match current sources/enrichment; run a full index rebuild`);
    }
    if (row.embedding_provider !== config.embedding?.provider ||
        row.embedding_model !== config.embedding?.model ||
        Number(row.embedding_dimensions) !== Number(config.embedding?.dimensions)) {
      throw new Error('[sync-state] table embedding metadata does not match configuration; run a full index rebuild');
    }
    expected.delete(row.id);
  }
  if (expected.size) throw new Error('[sync-state] table is missing current chunks; run a full index rebuild');
}

async function commandSyncState(config) {
  const built = buildChunks(config);
  const db = await openDb(config);
  const tableName = config.tableName || 'knowledge_chunks';
  let table;
  try { table = await db.openTable(tableName); }
  catch { throw new Error(`[sync-state] table ${tableName} is missing; run a full index rebuild`); }
  await validateSyncStateTable(config, table, built);
  const state = writeIndexState(config, built, { syncedFromValidatedSchemaV2Table: true });
  console.log(JSON.stringify({ ok: true, statePath: statePath(), docs: state.docs, chunks: state.chunks, files: Object.keys(state.files).length }, null, 2));
}

async function commandIncremental(config) {
  const built = buildChunks(config);
  const state = loadIndexState();
  const db = await openDb(config);
  const tableName = config.tableName || 'knowledge_chunks';
  let table;
  try { table = await db.openTable(tableName); }
  catch {
    console.error('[incremental] table missing; falling back to full index');
    return commandIndex(config);
  }

  if (!state || !state.files) {
    console.error('[incremental] state missing; falling back to one-time full index to establish baseline');
    return commandIndex(config);
  }
  if (state.version !== INDEX_SCHEMA_VERSION) {
    console.error(`[incremental] index schema ${state.version || 'legacy'} -> ${INDEX_SCHEMA_VERSION}; running a one-time full rebuild`);
    return commandIndex(config);
  }
  const vectorFingerprint = (embedding = {}) => JSON.stringify(embeddingIdentity(embedding));
  if (vectorFingerprint(state.embedding) !== vectorFingerprint(config.embedding)) {
    console.error('[incremental] embedding semantics changed; running a one-time full rebuild');
    return commandIndex(config);
  }

  const currentByPath = groupChunksByPath(built.chunks);
  const currentPaths = new Set(currentByPath.keys());
  const previousPaths = new Set(Object.keys(state.files));
  const removedPaths = [...previousPaths].filter((p) => !currentPaths.has(p));
  const semanticsChanged = state.buildFingerprint !== buildFingerprint(config);
  const changedPaths = [];
  for (const [p, chunks] of currentByPath) {
    const prev = state.files[p];
    const sha = chunks[0]?.file_sha256;
    if (!prev || prev.file_sha256 !== sha || semanticsChanged) changedPaths.push(p);
  }

  const changedChunks = changedPaths.flatMap((p) => currentByPath.get(p) || []);
  const deletePaths = [...new Set([...removedPaths, ...changedPaths])];
  if (deletePaths.length) await deleteSourcePaths(table, deletePaths);
  let rows = [];
  if (changedChunks.length) {
    rows = await rowsForChunks(config, changedChunks);
    if (rows.length) await table.add(rows);
  }

  const newState = writeIndexState(config, built, { lastIndexMode: 'incremental' });
  let rowCount = null;
  try { rowCount = await table.countRows(); } catch {}
  const report = {
    mode: 'incremental',
    indexedAt: new Date().toISOString(),
    tableName,
    dbPath: path.resolve(config.dbPath || './data/lancedb'),
    embedding: config.embedding,
    docs: built.docs.length,
    chunksAvailable: built.chunks.length,
    files: Object.keys(newState.files).length,
    changedFiles: changedPaths.length,
    removedFiles: removedPaths.length,
    changedChunks: changedChunks.length,
    addedChunks: rows.length,
    deletedPaths: deletePaths.length,
    rowsAfter: rowCount,
    skipped: built.skipped.length,
    secretHitFiles: built.secretHits.length
  };
  ensureDir('reports');
  fs.writeFileSync('reports/incremental-manifest.latest.json', JSON.stringify(report, null, 2));
  fs.writeFileSync(`reports/incremental-manifest.${nowStamp()}.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
}

async function commandCompactCache(config) {
  if (config.embedding?.provider !== 'qwen-local') throw new Error('Only qwen-local is supported');
  console.log(JSON.stringify({ ok: true, skipped: true, reason: 'qwen-local has no remote embedding cache' }));
}

async function commandStatus(config) {
  const privacy = privacyGate(config);
  const db = await openDb(config);
  const tableName = config.tableName || 'knowledge_chunks';
  let count = 0;
  let ok = false;
  try {
    const table = await db.openTable(tableName);
    count = await table.countRows();
    ok = true;
  } catch {}
  const manifestPath = 'reports/index-manifest.latest.json';
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, 'utf8')) : null;
  console.log(JSON.stringify({ ok, tableName, rows: count, manifest, privacy }, null, 2));
}

async function commandAudit(config) {
  const privacy = privacyGate(config);
  const built = buildChunks(config);
  const db = await openDb(config);
  const tableName = config.tableName || 'knowledge_chunks';
  let table;
  try { table = await db.openTable(tableName); }
  catch { throw new Error(`[audit] table ${tableName} is missing; run a full index rebuild`); }
  await validateSyncStateTable(config, table, built);
  const rows = await table.query().select([
    'source_type', 'deterministic_tags_json', 'ai_tags_json', 'ai_enrichment_status'
  ]).toArray();
  const sourceTypes = {};
  let deterministicTagsNonEmpty = 0;
  let aiTagsNonEmpty = 0;
  for (const row of rows) {
    const sourceType = row.source_type || 'unknown';
    sourceTypes[sourceType] = (sourceTypes[sourceType] || 0) + 1;
    try { if (JSON.parse(row.deterministic_tags_json || '[]').length) deterministicTagsNonEmpty += 1; } catch {}
    try {
      if (row.ai_enrichment_status === 'valid' && JSON.parse(row.ai_tags_json || '[]').length) aiTagsNonEmpty += 1;
    } catch {}
  }
  console.log(JSON.stringify({
    ok: true,
    tableName,
    docs: built.docs.length,
    chunks: built.chunks.length,
    rows: rows.length,
    sourceTypes,
    deterministicTagsNonEmpty,
    aiTagsNonEmpty,
    privacy,
    metadataExactMatch: true,
    embeddingIdentityMatch: true
  }, null, 2));
}


function rankTerms(text) {
  const t = String(text || '').toLowerCase();
  const terms = new Set();
  for (const m of t.matchAll(/[a-z0-9_\-]{2,}/g)) terms.add(m[0]);
  const cjk = Array.from(t.match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/gu) || []);
  for (let i = 0; i < cjk.length; i++) {
    terms.add(cjk[i]);
    if (i + 1 < cjk.length) terms.add(cjk[i] + cjk[i + 1]);
    if (i + 2 < cjk.length) terms.add(cjk[i] + cjk[i + 1] + cjk[i + 2]);
  }
  return [...terms].filter((x) => x.length > 1 || /[\p{Script=Han}]/u.test(x));
}

function isProgressQuery(query) {
  return /(現在|目前|做到哪|進度|狀態|最新|handoff|current|progress|status|next)/i.test(query);
}

function dateScore(date) {
  if (!date) return 0;
  const ts = Date.parse(date);
  if (!Number.isFinite(ts)) return 0;
  const days = Math.max(0, (Date.now() - ts) / 86400000);
  return Math.max(0, 1 - days / 120); // recent four months matter most for project state
}

function sourceProgressBoost(row) {
  const s = `${row.source_path || ''} ${row.title || ''} ${row.heading || ''}`.toLowerCase();
  let b = 0;
  if (/current_handoff|handoff|開發狀況|development_log|development_roadmap|development_schedule|project_/.test(s)) b += 0.25;
  if (/summary/.test(row.source_type || '')) b += 0.05;
  return b;
}

export function rerankRows(rows, query) {
  const qTerms = rankTerms(query);
  const progress = isProgressQuery(query);
  return rows.map((r) => {
    const trustedAiText = r.ai_enrichment_status === 'valid' ? `${r.ai_tags_json || ''} ${r.ai_summary || ''}` : '';
    const hay = `${r.project} ${r.title} ${r.heading} ${r.rel_path} ${r.deterministic_tags_json || ''} ${trustedAiText} ${r.chunk_text}`.toLowerCase();
    const overlap = qTerms.length ? qTerms.filter((t) => hay.includes(t.toLowerCase())).length / qTerms.length : 0;
    const vector = typeof r._distance === 'number' ? 1 / (1 + r._distance) : 0;
    const recency = progress ? dateScore(r.date) : 0;
    const progressBoost = progress ? sourceProgressBoost(r) : 0;
    const score = vector * 0.58 + overlap * 0.28 + recency * 0.09 + progressBoost;
    return { ...r, _rank_score: score, _keyword_overlap: overlap, _recency_score: recency };
  }).sort((a, b) => b._rank_score - a._rank_score);
}

function escapeSqlString(s) { return s.replaceAll("'", "''"); }

export function isRealDateSummaryRow(row) {
  if (row.source_type !== 'backup_summary') return false;
  const source = String(row.source_path || '').replace(/\\/g, '/');
  if (/_inventory-index/i.test(source)) return false;
  return /\/summary\/\d{4}-\d{2}-\d{2}\.md$/i.test(source);
}

async function searchRows(config, query, { limit = 5, project = '', realDateSummaryOnly = false } = {}) {
  const embeddingProvider = config.embedding?.provider || 'qwen-local';
  if (embeddingProvider !== 'qwen-local') throw new Error(`Only qwen-local is supported; got ${embeddingProvider}`);
  const queryVector = await getQwenEmbedder(config.embedding).embedOne(query);
  const db = await openDb(config);
  const table = await db.openTable(config.tableName || 'knowledge_chunks');
  let search = table.search(queryVector);
  if (project) search = search.where(`project = '${escapeSqlString(project)}'`);
  const fetchLimit = Math.max(limit * 20, 80);
  const rows = await search.limit(fetchLimit).toArray();
  const eligible = realDateSummaryOnly ? rows.filter(isRealDateSummaryRow) : rows;
  return rerankRows(eligible, query).slice(0, limit);
}

async function commandSearch(config) {
  const queryParts = process.argv.slice(3).filter((x, i, arr) => !arr[i - 1]?.startsWith('--') && !x.startsWith('--'));
  const query = queryParts.join(' ').trim() || arg('query', '');
  if (!query) throw new Error('Usage: npm run search -- "query" [-- --project BeanGo --limit 5]');
  const limit = Number(arg('limit', '5')) || 5;
  const project = arg('project', '');
  const realDateSummaryOnly = flag('real-date-summary-only');
  const rows = await searchRows(config, query, { limit, project, realDateSummaryOnly });
  console.log(`# Search: ${query}\n`);
  if (project) console.log(`Project filter: ${project}\n`);
  if (realDateSummaryOnly) console.log('Validation filter: real-date summary files only; synthetic inventory indexes rejected.\n');
  rows.forEach((r, idx) => {
    const distance = typeof r._distance === 'number' ? r._distance.toFixed(4) : 'n/a';
    const rank = typeof r._rank_score === 'number' ? r._rank_score.toFixed(4) : 'n/a';
    console.log(`## ${idx + 1}. ${r.project} / ${r.title} / ${r.heading}`);
    console.log(`- rank: ${rank}`);
    console.log(`- distance: ${distance}`);
    console.log(`- source: ${r.source_path}`);
    if (r.date) console.log(`- date: ${r.date}`);
    if (r.channel) console.log(`- channel: ${r.channel}`);
    const snippet = String(r.chunk_text || '').replace(/\s+/g, ' ').slice(0, 700);
    console.log(`- snippet: ${snippet}${snippet.length >= 700 ? '…' : ''}\n`);
  });
}

async function commandPrepareEnrichment(config) {
  const built = buildChunks(config);
  const limit = Number(arg('limit', '0')) || 0;
  const chunks = limit > 0 ? built.chunks.slice(0, limit) : built.chunks;
  const output = path.resolve(arg('output', './data/enrichment/input.jsonl'));
  ensureDir(path.dirname(output));
  const rows = chunks.map((chunk) => ({
    schema_version: 1,
    id: chunk.id,
    authoritative: {
      project: chunk.project,
      source_type: chunk.source_type,
      title: chunk.title,
      heading: chunk.heading,
      date: chunk.date,
      channel: chunk.channel
    },
    deterministic: {
      doc_type: chunk.deterministic_doc_type,
      tags: JSON.parse(chunk.deterministic_tags_json),
      importance: chunk.deterministic_importance
    },
    chunk_text: chunk.chunk_text
  }));
  fs.writeFileSync(output, rows.map((row) => JSON.stringify(row) + '\n').join(''));
  console.log(JSON.stringify({ ok: true, output, chunks: rows.length, note: 'Local JSONL only. External model use requires explicit privacy approval.' }, null, 2));
}

async function commandValidateEnrichment(config) {
  const input = arg('input', '');
  if (!input) throw new Error('Usage: npm run enrich:validate -- --input path/to/model-output.jsonl [--output path]');
  const output = arg('output', config.enrichment?.inputPath || './data/enrichment/validated.jsonl');
  const report = validateEnrichmentJsonl(input, output, { minConfidence: config.enrichment?.minConfidence ?? 0.75 });
  console.log(JSON.stringify(report, null, 2));
  if (report.invalid && !flag('allow-partial')) throw new Error(`Enrichment validation failed for ${report.invalid} row(s); validated rows were written but indexing is blocked without --allow-partial`);
}

async function commandBenchmark(config) {
  const file = path.resolve(arg('file', './config/benchmark.json'));
  if (!fs.existsSync(file)) throw new Error(`Benchmark file not found: ${file}`);
  const payload = JSON.parse(fs.readFileSync(file, 'utf8'));
  const cases = Array.isArray(payload) ? payload : payload.cases;
  const k = Number(arg('k', String(payload.k || 5))) || 5;
  const releaseGate = flag('release-gate');
  const results = new Map();
  for (const item of cases) {
    results.set(item.id, await searchRows(config, item.query, { limit: k, project: item.project || '' }));
  }
  const report = evaluateBenchmark(cases, results, { k, releaseGate });
  const thresholds = {
    minHitRate: Number(arg('min-hit-rate', String(payload.minHitRate ?? 0.8))),
    minMrr: Number(arg('min-mrr', String(payload.minMrr ?? 0.6)))
  };
  report.thresholds = thresholds;
  report.passed = benchmarkPasses(report, thresholds);
  report.generatedAt = new Date().toISOString();
  ensureDir('reports');
  const output = path.resolve(arg('output', './reports/benchmark.latest.json'));
  fs.writeFileSync(output, JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ ...report, output }, null, 2));
  if (releaseGate && !report.passed) throw new Error(`Benchmark gate failed: hitRate=${report.hitRate.toFixed(3)}, mrr=${report.mrr.toFixed(3)}`);
}

async function main() {
  const cmd = process.argv[2] || 'help';
  const config = loadConfig();
  if (cmd === 'scan') return commandScan(config);
  if (cmd === 'index') return commandIndex(config);
  if (cmd === 'incremental') return commandIncremental(config);
  if (cmd === 'sync-state') return commandSyncState(config);
  if (cmd === 'status') return commandStatus(config);
  if (cmd === 'audit') return commandAudit(config);
  if (cmd === 'search') return commandSearch(config);
  if (cmd === 'compact-cache') return commandCompactCache(config);
  if (cmd === 'prepare-enrichment') return commandPrepareEnrichment(config);
  if (cmd === 'validate-enrichment') return commandValidateEnrichment(config);
  if (cmd === 'benchmark') return commandBenchmark(config);
  if (cmd === 'profile') return console.log(JSON.stringify({ embedding: config.embedding, chunking: config.chunking, enrichment: enrichmentState(config), fullReindexRequiredWhenDimensionsChange: true }, null, 2));
  console.log(`knowledge-lancedb commands:\n  scan\n  index [--limit N] [--project NAME] [--append]\n  incremental\n  audit\n  search "query" [--project NAME] [--limit N]\n  prepare-enrichment [--output FILE] [--limit N]\n  validate-enrichment --input FILE [--output FILE]\n  benchmark --file FILE [--release-gate]\n  profile\n  status\n  compact-cache`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((err) => {
    console.error(err.stack || String(err));
    process.exit(1);
  });
}
