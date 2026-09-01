import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { validateEnrichmentRecord, applyAuxiliaryEnrichment, validateEnrichmentJsonl, loadEnrichmentCache } from '../src/enrichment.js';

const chunk = {
  id: 'chunk-1',
  project: 'VASO',
  source_type: 'project_doc',
  title: 'Release notes',
  heading: 'Decision',
  date: '2026-07-14',
  channel: 'engineering',
  chunk_text: 'The team chose the stable release.'
};

test('valid enrichment is normalized into auxiliary fields only', () => {
  const result = validateEnrichmentRecord({
    id: 'chunk-1',
    doc_type: 'decision',
    tags: ['release', 'Decision', 'release'],
    importance: 5,
    summary: 'Stable release selected.',
    decisions: ['Use the stable release.'],
    risks: ['Beta is unverified.'],
    action_items: ['Run CI.'],
    confidence: 0.91,
    model: 'any-model',
    schema_version: 1
  }, { minConfidence: 0.75 });
  assert.equal(result.ok, true);
  assert.equal(result.value.needs_review, false);
  assert.deepEqual(result.value.tags, ['release', 'decision']);

  const merged = applyAuxiliaryEnrichment(chunk, result.value);
  assert.equal(merged.project, 'VASO');
  assert.equal(merged.source_type, 'project_doc');
  assert.equal(merged.ai_doc_type, 'decision');
  assert.equal(merged.ai_enrichment_status, 'valid');
  assert.equal(merged.ai_confidence, 0.91);
  assert.deepEqual(JSON.parse(merged.ai_action_items_json), ['Run CI.']);
});

test('low-confidence enrichment is retained but always marked needs_review', () => {
  const result = validateEnrichmentRecord({
    id: 'chunk-1', doc_type: 'risk', tags: ['risk'], importance: 3,
    summary: 'Possible risk.', decisions: [], risks: ['Unknown impact.'], action_items: [],
    confidence: 0.4, model: 'any-model', schema_version: 1
  }, { minConfidence: 0.75 });
  assert.equal(result.ok, true);
  assert.equal(result.value.needs_review, true);
  const merged = applyAuxiliaryEnrichment(chunk, result.value);
  assert.equal(merged.ai_enrichment_status, 'low_confidence');
  assert.equal(merged.ai_needs_review, true);
});

test('enrichment cannot override authoritative deterministic fields', () => {
  const result = validateEnrichmentRecord({
    id: 'chunk-1', project: 'WrongProject', doc_type: 'decision', tags: [], importance: 2,
    summary: 'Attempted override.', decisions: [], risks: [], action_items: [], confidence: 0.9
  });
  assert.equal(result.ok, false);
  assert.match(result.error, /authoritative|forbidden/i);
});

test('missing enrichment uses a schema-stable fallback without mutating the chunk', () => {
  const merged = applyAuxiliaryEnrichment(chunk, null, { enabled: true });
  assert.equal(merged.project, 'VASO');
  assert.equal(merged.ai_enrichment_status, 'missing');
  assert.equal(merged.ai_doc_type, '');
  assert.equal(merged.ai_tags_json, '[]');
  assert.equal(merged.ai_needs_review, true);
});

test('validated JSONL can be loaded back as the indexing cache', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'enrichment-roundtrip-'));
  const input = path.join(dir, 'model-output.jsonl');
  const output = path.join(dir, 'validated.jsonl');
  fs.writeFileSync(input, JSON.stringify({
    id: 'chunk-1', doc_type: 'decision', tags: ['release'], importance: 4,
    summary: 'Stable release selected.', decisions: ['Use stable.'], risks: [], action_items: [],
    confidence: 0.82, model: 'any-model', schema_version: 1
  }) + '\n');
  const report = validateEnrichmentJsonl(input, output, { minConfidence: 0.75 });
  assert.equal(report.valid, 1);
  const cache = loadEnrichmentCache({
    enabled: true,
    inputPath: output,
    minConfidence: 0.75,
    privacyApprovedAt: '2026-07-14T00:00:00Z',
    privacyApprovedBy: 'test fixture'
  });
  assert.equal(cache.stats.invalid, 0);
  assert.equal(cache.records.get('chunk-1').doc_type, 'decision');
});

test('enabled enrichment fails closed without explicit privacy approval', () => {
  assert.throws(
    () => loadEnrichmentCache({ enabled: true, inputPath: '/tmp/not-needed.jsonl' }),
    /privacy approval/i
  );
});

test('enrichment requires exactly supported integer schema_version 1', () => {
  const base = {
    id: 'chunk-1', doc_type: 'decision', tags: [], importance: 3,
    summary: 'A decision.', decisions: [], risks: [], action_items: [], confidence: 0.9
  };
  for (const schemaVersion of [undefined, 2, 1.5, '1']) {
    const record = { ...base };
    if (schemaVersion !== undefined) record.schema_version = schemaVersion;
    const result = validateEnrichmentRecord(record);
    assert.equal(result.ok, false, `schema_version ${String(schemaVersion)} must fail`);
    assert.match(result.error, /schema_version/i);
  }
  assert.equal(validateEnrichmentRecord({ ...base, schema_version: 1 }).ok, true);
});
