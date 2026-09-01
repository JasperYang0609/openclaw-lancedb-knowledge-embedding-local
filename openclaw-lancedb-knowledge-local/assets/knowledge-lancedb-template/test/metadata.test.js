import test from 'node:test';
import assert from 'node:assert/strict';
import { deriveDeterministicMetadata } from '../src/metadata.js';

test('deterministic metadata classifies decisions without changing core metadata', () => {
  const chunk = {
    project: 'VASO',
    source_type: 'project_doc',
    title: 'Release decision',
    heading: 'Decision',
    chunk_text: 'Decision: keep the stable release. Do not deploy beta. Next action: verify CI.'
  };
  const meta = deriveDeterministicMetadata(chunk);
  assert.equal(meta.deterministic_doc_type, 'decision');
  assert.ok(JSON.parse(meta.deterministic_tags_json).includes('decision'));
  assert.ok(JSON.parse(meta.deterministic_tags_json).includes('action'));
  assert.ok(meta.deterministic_importance >= 4);
  assert.equal('project' in meta, false);
  assert.equal('source_type' in meta, false);
});

test('deterministic metadata is stable, bounded, and language-aware', () => {
  const chunk = {
    source_type: 'ops_doc',
    title: '故障處理 SOP',
    heading: '風險與修復',
    chunk_text: '發生 timeout 錯誤。根因是工具輸出過大。步驟：先備份，再修復，最後驗證。安全風險需記錄。'
  };
  const a = deriveDeterministicMetadata(chunk);
  const b = deriveDeterministicMetadata(chunk);
  assert.deepEqual(a, b);
  const tags = JSON.parse(a.deterministic_tags_json);
  assert.ok(tags.includes('procedure'));
  assert.ok(tags.includes('bug'));
  assert.ok(tags.includes('risk'));
  assert.ok(tags.length <= 12);
  assert.ok(a.deterministic_importance >= 1 && a.deterministic_importance <= 5);
});
