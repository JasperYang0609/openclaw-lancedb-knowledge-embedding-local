import test from 'node:test';
import assert from 'node:assert/strict';
import { rerankRows } from '../src/cli.js';

const base = {
  project: 'Fixture',
  title: 'Neutral title',
  heading: 'Notes',
  rel_path: 'notes.md',
  deterministic_tags_json: '[]',
  chunk_text: 'Neutral source text.',
  _distance: 0.2,
  date: ''
};

test('reranking ignores low-confidence AI text but accepts validated AI text', () => {
  const query = 'phoenix launch';
  const low = rerankRows([{
    ...base,
    ai_tags_json: '["phoenix"]',
    ai_summary: 'Phoenix launch plan.',
    ai_enrichment_status: 'low_confidence'
  }], query)[0];
  assert.equal(low._keyword_overlap, 0);

  const valid = rerankRows([{
    ...base,
    ai_tags_json: '["phoenix"]',
    ai_summary: 'Phoenix launch plan.',
    ai_enrichment_status: 'valid'
  }], query)[0];
  assert.ok(valid._keyword_overlap > 0);
});
