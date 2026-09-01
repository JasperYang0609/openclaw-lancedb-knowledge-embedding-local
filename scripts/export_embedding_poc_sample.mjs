#!/usr/bin/env node

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { createRequire } from 'node:module';

function arg(name, fallback = '') {
  const prefix = `--${name}=`;
  const item = process.argv.slice(2).find((value) => value.startsWith(prefix));
  return item ? item.slice(prefix.length) : fallback;
}

function requirePath(name) {
  const value = arg(name);
  if (!value) throw new Error(`Missing --${name}=PATH`);
  return path.resolve(value);
}

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function escapeSql(value) {
  return String(value).replaceAll("'", "''");
}

function expectedMatch(row, expected) {
  if (expected.project && row.project !== expected.project) return false;
  if (expected.sourcePathIncludes
    && !String(row.source_path || '').toLowerCase().includes(expected.sourcePathIncludes.toLowerCase())) return false;
  return true;
}

async function readGeminiQueries(cachePath, benchmark, embedding) {
  const wanted = new Map();
  for (const item of benchmark.cases) {
    const key = sha256(`${embedding.model}\n${embedding.dimensions}\n${embedding.queryTaskType}\n${item.query}`);
    wanted.set(key, item.id);
  }
  const found = new Map();
  const stream = fs.createReadStream(cachePath, { encoding: 'utf8' });
  const lines = readline.createInterface({ input: stream, crlfDelay: Infinity });
  for await (const line of lines) {
    if (!line) continue;
    let row;
    try { row = JSON.parse(line); } catch { continue; }
    const id = wanted.get(row.key);
    if (id && Array.isArray(row.vector)) found.set(id, row.vector);
  }
  const missing = benchmark.cases.map((item) => item.id).filter((id) => !found.has(id));
  if (missing.length) throw new Error(`Gemini cache is missing ${missing.length} benchmark query vector(s)`);
  return Object.fromEntries(found);
}

async function main() {
  const liveRoot = requirePath('live-root');
  const benchmarkPath = requirePath('benchmark');
  const outputPath = requirePath('output');
  const sampleSize = Number(arg('sample-size', '1000'));
  if (!Number.isInteger(sampleSize) || sampleSize < 100 || sampleSize > 10000) {
    throw new Error('sample-size must be an integer from 100 through 10000');
  }

  const requireFromLive = createRequire(path.join(liveRoot, 'package.json'));
  const lancedb = requireFromLive('@lancedb/lancedb');
  const benchmark = JSON.parse(fs.readFileSync(benchmarkPath, 'utf8'));
  const config = JSON.parse(fs.readFileSync(path.join(liveRoot, 'config/source-map.json'), 'utf8'));
  const db = await lancedb.connect(path.join(liveRoot, 'data/lancedb'));
  const table = await db.openTable(config.tableName || 'knowledge_chunks');

  const metadata = await table.query()
    .select(['id', 'project', 'source_path'])
    .limit(200000)
    .toArray();
  const goldIds = new Set();
  for (const item of benchmark.cases) {
    for (const row of metadata) if (expectedMatch(row, item.expected)) goldIds.add(row.id);
  }
  if (goldIds.size >= sampleSize) throw new Error(`Gold rows (${goldIds.size}) must be smaller than sample size (${sampleSize})`);

  const distractors = metadata
    .filter((row) => !goldIds.has(row.id))
    .sort((a, b) => sha256(`qwen-gemini-poc-v1\n${a.id}`).localeCompare(sha256(`qwen-gemini-poc-v1\n${b.id}`)))
    .slice(0, sampleSize - goldIds.size)
    .map((row) => row.id);
  const selectedIds = [...goldIds, ...distractors];

  const columns = [
    'id', 'source_path', 'rel_path', 'source_type', 'project', 'title', 'heading', 'date',
    'chunk_text', 'deterministic_tags_json', 'ai_tags_json', 'ai_summary',
    'ai_enrichment_status', 'vector'
  ];
  const rows = [];
  for (let start = 0; start < selectedIds.length; start += 100) {
    const batch = selectedIds.slice(start, start + 100);
    const filter = `id IN (${batch.map((id) => `'${escapeSql(id)}'`).join(',')})`;
    rows.push(...await table.query().where(filter).select(columns).limit(batch.length).toArray());
  }
  if (rows.length !== sampleSize) throw new Error(`Expected ${sampleSize} exported rows, got ${rows.length}`);
  const byId = new Map(rows.map((row) => [row.id, row]));
  const orderedRows = selectedIds.map((id) => byId.get(id));
  if (orderedRows.some((row) => !row)) throw new Error('Exported sample is missing selected IDs');

  const embedding = config.embedding;
  if (embedding.provider !== 'google-gemini' || embedding.dimensions !== 768) {
    throw new Error('POC exporter requires the current 768-dimensional Gemini index');
  }
  const cachePath = arg('gemini-query-cache')
    ? path.resolve(arg('gemini-query-cache'))
    : path.resolve(liveRoot, embedding.cachePath);
  const geminiQueries = await readGeminiQueries(cachePath, benchmark, embedding);

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify({
    schemaVersion: 1,
    selection: {
      seed: 'qwen-gemini-poc-v1',
      totalCorpusRows: metadata.length,
      sampleSize,
      goldRows: goldIds.size,
      distractorRows: distractors.length
    },
    embedding: {
      provider: embedding.provider,
      model: embedding.model,
      dimensions: embedding.dimensions,
      documentTaskType: embedding.documentTaskType,
      queryTaskType: embedding.queryTaskType
    },
    benchmark,
    geminiQueries,
    rows: orderedRows
  }));
  console.log(JSON.stringify({
    ok: true,
    totalCorpusRows: metadata.length,
    sampleSize,
    goldRows: goldIds.size,
    distractorRows: distractors.length,
    benchmarkCases: benchmark.cases.length,
    geminiQueryVectors: Object.keys(geminiQueries).length
  }, null, 2));
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
