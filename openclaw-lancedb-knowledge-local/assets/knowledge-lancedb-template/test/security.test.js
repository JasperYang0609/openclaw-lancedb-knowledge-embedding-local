import test from 'node:test';
import assert from 'node:assert/strict';
import { redactSecrets } from '../src/security.js';

test('redacts common secret-like tokens before embedding/indexing', () => {
  const notionLike = 'ntn_' + 'abcdefghijklmnopqrstuvwxyz123456';
  const openaiLike = 'sk-' + 'proj-' + 'abcdefghijklmnopqrstuvwxyz123456';
  const input = `Token: ${notionLike} and key ${openaiLike}`;
  const out = redactSecrets(input);
  assert.match(out.text, /REDACTED/);
  assert.ok(out.hits.reduce((a, h) => a + h.count, 0) >= 2);
  assert.doesNotMatch(out.text, /sk-proj-/);
  assert.doesNotMatch(out.text, /ntn_/);
});

test('redacts extended provider tokens and credentials', () => {
  // Build samples via string concatenation so the test file itself does not trip secret scanners.
  const samples = [
    'ghp_' + 'abcdefghijklmnopqrstuvwxyz0123456789',
    'github_pat_' + '11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz',
    'AKIA' + 'ABCDEFGHIJKLMNOP',
    'xoxb-' + '1234567890-abcdefghijklmnop',
    'MTA5' + 'MDAwMDAwMDAwMDAwMDAwMDA' + '.GaBcDe.' + 'abcdefghijklmnopqrstuvwxyz0',
    '123456789' + ':AA' + 'abcdefghijklmnopqrstuvwxyz0123456',
    'sk_live_' + 'abcdefghijklmnop0123',
    'https://user:' + 'supersecret@example.com/path',
    '-----BEGIN ' + 'PRIVATE KEY-----\nfake-key-material\n-----END ' + 'PRIVATE KEY-----',
    '密碼:' + '超級機密123',
    '金鑰: ' + 'abc-def-123',
    '密碼：' + '全形冒號機密456',
    '金鑰：' + 'abc-def-789',
    '憑證： ' + 'cert-secret-000'
  ];
  for (const sample of samples) {
    const out = redactSecrets(`context ${sample} tail`);
    assert.match(out.text, /REDACTED/, `expected redaction for: ${sample}`);
    assert.ok(out.hits.length >= 1, `expected hit recorded for: ${sample}`);
  }
});

test('does not redact plain urls without embedded credentials', () => {
  const out = redactSecrets('see https://example.com/docs and http://localhost:3000/health');
  assert.doesNotMatch(out.text, /REDACTED_URL_BASIC_AUTH/);
});
