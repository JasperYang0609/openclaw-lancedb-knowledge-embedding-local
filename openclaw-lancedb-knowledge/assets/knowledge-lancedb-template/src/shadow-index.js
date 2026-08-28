#!/usr/bin/env node

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as lancedb from '@lancedb/lancedb';
import { loadConfig, buildChunks } from './sources.js';
import { loadEnrichmentCache, applyAuxiliaryEnrichment } from './enrichment.js';
import { getQwenEmbedder } from './embed-qwen.js';

const CHECKPOINT_SCHEMA_VERSION = 1;

function nowStamp() {
  return new Date().toISOString().replace(/[:.]/g, '-');
}

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative.length > 0 && !relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative);
}

export function assertShadowConfig(config) {
  if (config.shadow?.enabled !== true) throw new Error('Shadow indexing requires shadow.enabled=true');
  if (config.embedding?.provider !== 'qwen-local') throw new Error('Shadow indexing requires the qwen-local provider');
  const root = path.resolve(config.shadow.root || '');
  if (!config.shadow?.root || root === path.parse(root).root) throw new Error('Shadow root must be a specific non-root directory');
  const dbPath = path.resolve(config.dbPath || '');
  if (!isInside(root, dbPath)) throw new Error('Shadow dbPath must stay inside shadow root');
  const forbidden = (config.shadow.forbiddenPaths || []).map((item) => path.resolve(item));
  for (const target of [root, dbPath]) {
    if (forbidden.some((blocked) => target === blocked || isInside(blocked, target) || isInside(target, blocked))) {
      throw new Error('Shadow path overlaps a forbidden Production path');
    }
  }
  const rawConfigured = (config.sources || []).some((source) => source.sourceType === 'discord_raw');
  if (rawConfigured && config.privacy?.discordRawApproval !== 'LOCAL_ONLY') {
    throw new Error('Local Qwen shadow indexing of discord_raw requires privacy.discordRawApproval=LOCAL_ONLY');
  }
  return { root, dbPath };
}

export function corpusFingerprint(chunks) {
  const entries = chunks.map((chunk) => {
    if (!chunk.id || !chunk.content_sha256) throw new Error('Corpus fingerprint requires id and content_sha256');
    return `${chunk.id}\0${chunk.content_sha256}`;
  }).sort();
  const hash = crypto.createHash('sha256');
  for (const entry of entries) hash.update(entry).update('\n');
  return hash.digest('hex');
}

export function readCheckpoint(file) {
  if (!fs.existsSync(file)) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (parsed.schemaVersion !== CHECKPOINT_SCHEMA_VERSION) throw new Error('schema');
    return parsed;
  } catch {
    throw new Error('Invalid checkpoint JSON or schema');
  }
}

export function writeCheckpointAtomic(file, checkpoint) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(checkpoint, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(temporary, file);
}

function embeddingIdentity(config) {
  const embedding = config.embedding || {};
  return {
    provider: embedding.provider,
    model: embedding.model,
    dimensions: embedding.dimensions,
    nativeDimensions: embedding.nativeDimensions,
    quantization: embedding.quantization,
    modelSha256: embedding.modelSha256,
    runtimeRevision: embedding.runtimeRevision,
    pooling: embedding.pooling,
    queryInstruction: embedding.queryInstruction,
    normalization: embedding.normalization
  };
}

function embeddingFingerprint(config) {
  return crypto.createHash('sha256').update(JSON.stringify(embeddingIdentity(config))).digest('hex');
}

function chunkEmbedText(chunk) {
  return `${chunk.project}\n${chunk.title}\n${chunk.heading}\n${chunk.chunk_text}`;
}

async function rowsForBatch(config, chunks, embedder, enrichment) {
  const prepared = chunks.map((chunk) => applyAuxiliaryEnrichment(
    chunk,
    enrichment.records.get(chunk.id) || null,
    { enabled: enrichment.enabled }
  ));
  const vectors = await embedder.embedDocuments(prepared.map(chunkEmbedText));
  return prepared.map((chunk, index) => ({
    ...chunk,
    embedding_provider: config.embedding.provider,
    embedding_model: config.embedding.model,
    embedding_dimensions: config.embedding.dimensions,
    vector: vectors[index]
  }));
}

function stateFiles(chunks) {
  const files = {};
  for (const chunk of chunks) {
    if (!files[chunk.source_path]) {
      files[chunk.source_path] = {
        source_path: chunk.source_path,
        rel_path: chunk.rel_path,
        source_id: chunk.source_id,
        source_type: chunk.source_type,
        project: chunk.project,
        channel: chunk.channel,
        file_sha256: chunk.file_sha256,
        file_mtime_ms: chunk.file_mtime_ms,
        file_bytes: chunk.file_bytes,
        chunk_ids: [],
        chunks: 0
      };
    }
    files[chunk.source_path].chunk_ids.push(chunk.id);
    files[chunk.source_path].chunks += 1;
  }
  return files;
}

async function tableOrNull(db, tableName) {
  try { return await db.openTable(tableName); }
  catch { return null; }
}

async function tableIds(table) {
  if (!table) return [];
  return (await table.query().select(['id']).toArray()).map((row) => row.id);
}

function progressLine(payload) {
  process.stderr.write(JSON.stringify({ type: 'qwen-shadow-progress', at: new Date().toISOString(), ...payload }) + '\n');
}

export async function runShadowIndex(config, { limit = 0, indexBatchSize = 64 } = {}) {
  const { root, dbPath } = assertShadowConfig(config);
  if (!Number.isInteger(indexBatchSize) || indexBatchSize < 1 || indexBatchSize > 512) {
    throw new Error('indexBatchSize must be an integer from 1 through 512');
  }
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(path.join(root, 'reports'), { recursive: true });
  const checkpointPath = path.join(root, 'checkpoint.json');
  const manifestPath = path.join(root, 'reports', 'shadow-index-manifest.latest.json');
  const statePath = path.join(root, 'data', 'index-state.json');

  const built = buildChunks(config);
  const chunks = limit > 0 ? built.chunks.slice(0, limit) : built.chunks;
  if (!chunks.length) throw new Error('No chunks available for Qwen shadow indexing');
  const ids = new Set(chunks.map((chunk) => chunk.id));
  if (ids.size !== chunks.length) throw new Error('Source corpus contains duplicate chunk ids');
  const corpusSha256 = corpusFingerprint(chunks);
  const embeddingSha256 = embeddingFingerprint(config);
  const checkpoint = readCheckpoint(checkpointPath);
  if (checkpoint && checkpoint.corpusFingerprint !== corpusSha256) {
    throw new Error('Current corpus does not match the durable shadow checkpoint');
  }
  if (checkpoint && checkpoint.embeddingFingerprint !== embeddingSha256) {
    throw new Error('Embedding identity does not match the durable shadow checkpoint');
  }

  fs.mkdirSync(dbPath, { recursive: true });
  const db = await lancedb.connect(dbPath);
  const tableName = config.tableName || 'knowledge_chunks_qwen_shadow';
  let table = await tableOrNull(db, tableName);
  if (checkpoint && !table) throw new Error('Checkpoint exists but the shadow LanceDB table is missing');
  const existingIdList = await tableIds(table);
  const existingIds = new Set(existingIdList);
  if (existingIds.size !== existingIdList.length) throw new Error('Shadow table already contains duplicate chunk ids');
  for (const id of existingIds) {
    if (!ids.has(id)) throw new Error('Shadow table contains a chunk id outside the frozen corpus');
  }

  const embedder = getQwenEmbedder(config.embedding);
  const enrichment = loadEnrichmentCache(config.enrichment || {});
  const startedAt = checkpoint?.startedAt || new Date().toISOString();
  const startedMs = Date.parse(startedAt);
  let completed = existingIds.size;
  writeCheckpointAtomic(checkpointPath, {
    schemaVersion: CHECKPOINT_SCHEMA_VERSION,
    status: 'running',
    startedAt,
    updatedAt: new Date().toISOString(),
    corpusFingerprint: corpusSha256,
    embeddingFingerprint: embeddingSha256,
    totalRows: chunks.length,
    completedRows: completed,
    tableName,
    dbPath
  });
  progressLine({ phase: 'resume', completedRows: completed, totalRows: chunks.length });

  for (let start = 0; start < chunks.length; start += indexBatchSize) {
    const candidates = chunks.slice(start, start + indexBatchSize).filter((chunk) => !existingIds.has(chunk.id));
    if (!candidates.length) continue;
    const rows = await rowsForBatch(config, candidates, embedder, enrichment);
    if (!table) table = await db.createTable(tableName, rows, { mode: 'overwrite' });
    else await table.add(rows);
    for (const chunk of candidates) existingIds.add(chunk.id);
    completed = existingIds.size;
    const elapsedSeconds = Math.max(1, (Date.now() - startedMs) / 1000);
    const rowsPerSecond = completed / elapsedSeconds;
    const etaSeconds = rowsPerSecond > 0 ? Math.max(0, (chunks.length - completed) / rowsPerSecond) : null;
    writeCheckpointAtomic(checkpointPath, {
      schemaVersion: CHECKPOINT_SCHEMA_VERSION,
      status: 'running',
      startedAt,
      updatedAt: new Date().toISOString(),
      corpusFingerprint: corpusSha256,
      embeddingFingerprint: embeddingSha256,
      totalRows: chunks.length,
      completedRows: completed,
      rowsPerSecond,
      etaSeconds,
      tableName,
      dbPath
    });
    progressLine({ phase: 'index', completedRows: completed, totalRows: chunks.length, rowsPerSecond, etaSeconds });
  }

  if (!table) throw new Error('Shadow table was not created');
  const finalRows = await table.query().select([
    'id', 'embedding_provider', 'embedding_model', 'embedding_dimensions'
  ]).toArray();
  const finalIds = new Set(finalRows.map((row) => row.id));
  if (finalRows.length !== chunks.length || finalIds.size !== chunks.length) {
    throw new Error(`Shadow reconciliation failed: rows=${finalRows.length}, unique=${finalIds.size}, expected=${chunks.length}`);
  }
  if (finalRows.some((row) => row.embedding_provider !== config.embedding.provider
    || row.embedding_model !== config.embedding.model
    || Number(row.embedding_dimensions) !== Number(config.embedding.dimensions))) {
    throw new Error('Shadow reconciliation found mixed embedding identity metadata');
  }
  const schema = await table.schema();
  const vectorField = schema.fields.find((field) => field.name === 'vector');
  if (Number(vectorField?.type?.listSize) !== Number(config.embedding.dimensions)) {
    throw new Error('Shadow reconciliation found an unexpected vector dimension');
  }

  const completedAt = new Date().toISOString();
  const manifest = {
    schemaVersion: 1,
    status: 'complete',
    startedAt,
    completedAt,
    docs: built.docs.length,
    chunksAvailable: built.chunks.length,
    chunksIndexed: chunks.length,
    skipped: built.skipped.length,
    secretHitFiles: built.secretHits.length,
    corpusFingerprint: corpusSha256,
    embeddingFingerprint: embeddingSha256,
    embedding: embeddingIdentity(config),
    tableName,
    dbPath,
    rows: finalRows.length,
    uniqueChunkIds: finalIds.size,
    vectorDimensions: config.embedding.dimensions
  };
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n');
  fs.writeFileSync(path.join(root, 'reports', `shadow-index-manifest.${nowStamp()}.json`), JSON.stringify(manifest, null, 2) + '\n');
  const files = stateFiles(chunks);
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  fs.writeFileSync(statePath, JSON.stringify({
    version: 2,
    updatedAt: completedAt,
    tableName,
    dbPath,
    embedding: config.embedding,
    chunking: config.chunking,
    docs: Object.keys(files).length,
    chunks: chunks.length,
    corpusFingerprint: corpusSha256,
    embeddingFingerprint: embeddingSha256,
    files
  }, null, 2) + '\n');
  writeCheckpointAtomic(checkpointPath, {
    schemaVersion: CHECKPOINT_SCHEMA_VERSION,
    status: 'complete',
    startedAt,
    updatedAt: completedAt,
    corpusFingerprint: corpusSha256,
    embeddingFingerprint: embeddingSha256,
    totalRows: chunks.length,
    completedRows: chunks.length,
    tableName,
    dbPath,
    manifestPath,
    statePath
  });
  progressLine({ phase: 'complete', completedRows: chunks.length, totalRows: chunks.length, manifestPath });
  return manifest;
}

function numericArg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  if (index < 0) return fallback;
  return Number(process.argv[index + 1]);
}

async function main() {
  const config = loadConfig();
  const limit = numericArg('limit', 0) || 0;
  const indexBatchSize = numericArg('index-batch-size', config.shadow?.indexBatchSize || 64);
  const manifest = await runShadowIndex(config, { limit, indexBatchSize });
  console.log(JSON.stringify(manifest, null, 2));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error.stack || String(error));
    process.exit(1);
  });
}
