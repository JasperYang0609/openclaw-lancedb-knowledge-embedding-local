import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { assertShadowConfig, corpusFingerprint, readCheckpoint, writeCheckpointAtomic } from '../src/shadow-index.js';

function fixtureConfig(root, overrides = {}) {
  return {
    dbPath: path.join(root, 'data', 'lancedb'),
    tableName: 'knowledge_chunks_qwen_shadow',
    embedding: { provider: 'qwen-local', model: 'Qwen3-Embedding-4B-Q5_K_M', dimensions: 768 },
    shadow: { enabled: true, root, forbiddenPaths: [] },
    ...overrides
  };
}

test('shadow config requires qwen-local and confines all writes to the shadow root', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'shadow-guard-'));
  assert.doesNotThrow(() => assertShadowConfig(fixtureConfig(root)));
  assert.throws(
    () => assertShadowConfig(fixtureConfig(root, { dbPath: path.join(root, '..', 'production') })),
    /inside shadow root/i
  );
  assert.throws(
    () => assertShadowConfig(fixtureConfig(root, { embedding: { provider: 'google-gemini' } })),
    /qwen-local/i
  );
  assert.throws(
    () => assertShadowConfig(fixtureConfig(root, { shadow: { enabled: false, root, forbiddenPaths: [] } })),
    /enabled/i
  );
  const forbidden = path.join(root, 'data', 'lancedb');
  assert.throws(
    () => assertShadowConfig(fixtureConfig(root, { shadow: { enabled: true, root, forbiddenPaths: [forbidden] } })),
    /forbidden/i
  );
});

test('corpus fingerprint is deterministic and changes with ids or content', () => {
  const chunks = [
    { id: 'b', content_sha256: '2' },
    { id: 'a', content_sha256: '1' }
  ];
  assert.equal(corpusFingerprint(chunks), corpusFingerprint([...chunks].reverse()));
  assert.notEqual(corpusFingerprint(chunks), corpusFingerprint([{ id: 'a', content_sha256: 'changed' }, chunks[0]]));
  assert.throws(() => corpusFingerprint([{ id: 'a' }]), /content_sha256/i);
});

test('checkpoint writes atomically and rejects invalid JSON', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'shadow-checkpoint-'));
  const file = path.join(root, 'checkpoint.json');
  const checkpoint = { schemaVersion: 1, status: 'running', completedRows: 20, corpusFingerprint: 'abc' };
  writeCheckpointAtomic(file, checkpoint);
  assert.deepEqual(readCheckpoint(file), checkpoint);
  assert.equal(fs.existsSync(`${file}.tmp`), false);
  fs.writeFileSync(file, '{bad-json');
  assert.throws(() => readCheckpoint(file), /invalid checkpoint/i);
});
