#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { performance } from 'node:perf_hooks';

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

function l2Normalize(vector) {
  let sum = 0;
  for (const value of vector) sum += value * value;
  const norm = Math.sqrt(sum);
  if (!Number.isFinite(norm) || norm === 0) throw new Error('Embedding norm is not finite and non-zero');
  return vector.map((value) => value / norm);
}

function truncateAndNormalize(vector, dimensions) {
  if (!Array.isArray(vector) || vector.length < dimensions) throw new Error(`Expected at least ${dimensions} dimensions`);
  return l2Normalize(vector.slice(0, dimensions));
}

function dot(a, b) {
  let result = 0;
  for (let i = 0; i < a.length; i += 1) result += a[i] * b[i];
  return result;
}

function percentile(values, quantile) {
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(quantile * sorted.length) - 1));
  return sorted[index];
}

function rankTerms(text) {
  const lowered = String(text || '').toLowerCase();
  const terms = new Set();
  for (const match of lowered.matchAll(/[a-z0-9_\-]{2,}/g)) terms.add(match[0]);
  const cjk = Array.from(lowered.match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/gu) || []);
  for (let i = 0; i < cjk.length; i += 1) {
    terms.add(cjk[i]);
    if (i + 1 < cjk.length) terms.add(cjk[i] + cjk[i + 1]);
    if (i + 2 < cjk.length) terms.add(cjk[i] + cjk[i + 1] + cjk[i + 2]);
  }
  return [...terms].filter((value) => value.length > 1 || /[\p{Script=Han}]/u.test(value));
}

function isProgressQuery(query) {
  return /(現在|目前|做到哪|進度|狀態|最新|handoff|current|progress|status|next)/i.test(query);
}

function dateScore(date) {
  if (!date) return 0;
  const timestamp = Date.parse(date);
  if (!Number.isFinite(timestamp)) return 0;
  const days = Math.max(0, (Date.now() - timestamp) / 86400000);
  return Math.max(0, 1 - days / 120);
}

function sourceProgressBoost(row) {
  const text = `${row.source_path || ''} ${row.title || ''} ${row.heading || ''}`.toLowerCase();
  let result = 0;
  if (/current_handoff|handoff|開發狀況|development_log|development_roadmap|development_schedule|project_/.test(text)) result += 0.25;
  if (/summary/.test(row.source_type || '')) result += 0.05;
  return result;
}

function rerankRows(rows, query) {
  const terms = rankTerms(query);
  const progress = isProgressQuery(query);
  return rows.map((row) => {
    const trustedAiText = row.ai_enrichment_status === 'valid' ? `${row.ai_tags_json || ''} ${row.ai_summary || ''}` : '';
    const haystack = `${row.project} ${row.title} ${row.heading} ${row.rel_path} ${row.deterministic_tags_json || ''} ${trustedAiText} ${row.chunk_text}`.toLowerCase();
    const overlap = terms.length ? terms.filter((term) => haystack.includes(term.toLowerCase())).length / terms.length : 0;
    const vectorScore = 1 / (1 + (1 - row._similarity));
    const recency = progress ? dateScore(row.date) : 0;
    const progressBoost = progress ? sourceProgressBoost(row) : 0;
    return { ...row, _rankScore: vectorScore * 0.58 + overlap * 0.28 + recency * 0.09 + progressBoost };
  }).sort((a, b) => b._rankScore - a._rankScore);
}

function matchesExpected(row, expected) {
  if (expected.project && String(row.project).toLowerCase() !== expected.project.toLowerCase()) return false;
  if (expected.sourcePathIncludes
    && !String(row.source_path || '').toLowerCase().includes(expected.sourcePathIncludes.toLowerCase())) return false;
  return true;
}

function evaluateProfile({ name, rows, docVectors, queryVectors, benchmark, hybrid }) {
  const details = [];
  let hits = 0;
  let reciprocalRank = 0;
  for (const item of benchmark.cases) {
    const queryVector = queryVectors[item.id];
    let candidates = rows.map((row, index) => ({ ...row, _similarity: dot(queryVector, docVectors[index]) }))
      .sort((a, b) => b._similarity - a._similarity);
    if (hybrid) candidates = rerankRows(candidates.slice(0, 100), item.query);
    const top = candidates.slice(0, benchmark.k || 5);
    const found = top.findIndex((row) => matchesExpected(row, item.expected));
    const rank = found >= 0 ? found + 1 : null;
    if (rank) { hits += 1; reciprocalRank += 1 / rank; }
    details.push({ id: item.id, rank, hit: Boolean(rank), topSourceBasenames: top.map((row) => path.basename(row.source_path || '')) });
  }
  return {
    name,
    mode: hybrid ? 'hybrid_rerank' : 'pure_vector',
    total: benchmark.cases.length,
    k: benchmark.k || 5,
    hits,
    hitRate: hits / benchmark.cases.length,
    mrr: reciprocalRank / benchmark.cases.length,
    cases: details
  };
}

async function embed(server, apiKey, inputs) {
  const response = await fetch(`${server}/v1/embeddings`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ input: inputs, encoding_format: 'float' })
  });
  const body = await response.text();
  if (!response.ok) throw new Error(`Embedding server HTTP ${response.status}: ${body.slice(0, 300)}`);
  const parsed = JSON.parse(body);
  const ordered = [...parsed.data].sort((a, b) => a.index - b.index).map((item) => item.embedding);
  if (ordered.length !== inputs.length) throw new Error(`Embedding count mismatch: ${ordered.length} vs ${inputs.length}`);
  return ordered;
}

async function main() {
  const inputPath = requirePath('input');
  const outputPath = requirePath('output');
  const server = arg('server', 'http://127.0.0.1:18888');
  const apiKey = arg('api-key', 'qwen-poc-local-20260825');
  const batchSize = Number(arg('batch-size', '4'));
  const quantization = arg('quantization', 'Q5_K_M');
  const quantizationSlug = quantization.toLowerCase().replaceAll('_', '');
  if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 32) {
    throw new Error('batch-size must be an integer from 1 through 32');
  }
  const serverUrl = new URL(server);
  if (serverUrl.protocol !== 'http:' || !['127.0.0.1', 'localhost', '::1'].includes(serverUrl.hostname)) {
    throw new Error('POC embedding server must use loopback HTTP');
  }
  const payload = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
  const { rows, benchmark } = payload;
  const geminiDocs = rows.map((row) => l2Normalize(row.vector));
  const geminiQueries = Object.fromEntries(Object.entries(payload.geminiQueries).map(([id, vector]) => [id, l2Normalize(vector)]));

  const qwenNativeDocs = [];
  const documentCallMs = [];
  const startedAt = performance.now();
  for (let start = 0; start < rows.length; start += batchSize) {
    const batch = rows.slice(start, start + batchSize).map((row) => `${row.project}\n${row.title}\n${row.heading}\n${row.chunk_text}`);
    const callStarted = performance.now();
    qwenNativeDocs.push(...await embed(server, apiKey, batch));
    documentCallMs.push(performance.now() - callStarted);
    if ((start + batch.length) % 100 === 0 || start + batch.length === rows.length) {
      console.error(`[qwen-poc] embedded ${start + batch.length}/${rows.length}`);
    }
  }
  const documentElapsedMs = performance.now() - startedAt;
  const qwen2560Docs = qwenNativeDocs.map((vector) => truncateAndNormalize(vector, 2560));
  const qwen768Docs = qwenNativeDocs.map((vector) => truncateAndNormalize(vector, 768));

  const qwen2560Queries = {};
  const qwen768Queries = {};
  const queryLatencyMs = [];
  const instruction = 'Given a web search query, retrieve relevant passages that answer the query';
  for (const item of benchmark.cases) {
    const queryText = `Instruct: ${instruction}\nQuery:${item.query}`;
    const queryStarted = performance.now();
    const [vector] = await embed(server, apiKey, [queryText]);
    queryLatencyMs.push(performance.now() - queryStarted);
    qwen2560Queries[item.id] = truncateAndNormalize(vector, 2560);
    qwen768Queries[item.id] = truncateAndNormalize(vector, 768);
  }
  const firstCase = benchmark.cases[0];
  const repeatText = `Instruct: ${instruction}\nQuery:${firstCase.query}`;
  const [repeatVector] = await embed(server, apiKey, [repeatText]);
  const repeatNormalized = truncateAndNormalize(repeatVector, 2560);
  const maxRepeatAbsDiff = Math.max(...repeatNormalized.map((value, index) => Math.abs(value - qwen2560Queries[firstCase.id][index])));

  const profiles = [
    { name: 'gemini-embedding-001-768', docs: geminiDocs, queries: geminiQueries },
    { name: `qwen3-embedding-4b-${quantizationSlug}-768`, docs: qwen768Docs, queries: qwen768Queries },
    { name: `qwen3-embedding-4b-${quantizationSlug}-2560`, docs: qwen2560Docs, queries: qwen2560Queries }
  ];
  const evaluations = [];
  for (const profile of profiles) {
    evaluations.push(evaluateProfile({ name: profile.name, rows, docVectors: profile.docs, queryVectors: profile.queries, benchmark, hybrid: false }));
    evaluations.push(evaluateProfile({ name: profile.name, rows, docVectors: profile.docs, queryVectors: profile.queries, benchmark, hybrid: true }));
  }

  const report = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    sample: payload.selection,
    benchmarkCases: benchmark.cases.length,
    qwenRuntime: {
      quantization,
      nativeDimensions: 2560,
      documentElapsedMs,
      documentsPerSecond: rows.length / (documentElapsedMs / 1000),
      documentBatchSize: batchSize,
      documentCallP50Ms: percentile(documentCallMs, 0.5),
      documentCallP95Ms: percentile(documentCallMs, 0.95),
      queryP50Ms: percentile(queryLatencyMs, 0.5),
      queryP95Ms: percentile(queryLatencyMs, 0.95),
      repeatMaxAbsDiff: maxRepeatAbsDiff
    },
    correctness: {
      qwenVectorCount: qwenNativeDocs.length,
      qwenDimensions: qwenNativeDocs[0]?.length,
      allFinite: qwenNativeDocs.every((vector) => vector.every(Number.isFinite)),
      nativeNormMin: Math.min(...qwen2560Docs.map((vector) => Math.sqrt(dot(vector, vector)))),
      nativeNormMax: Math.max(...qwen2560Docs.map((vector) => Math.sqrt(dot(vector, vector))))
    },
    evaluations
  };
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify(report, null, 2));
  console.log(JSON.stringify({
    ok: true,
    sample: report.sample,
    qwenRuntime: report.qwenRuntime,
    correctness: report.correctness,
    evaluations: evaluations.map(({ name, mode, hitRate, mrr }) => ({ name, mode, hitRate, mrr }))
  }, null, 2));
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
