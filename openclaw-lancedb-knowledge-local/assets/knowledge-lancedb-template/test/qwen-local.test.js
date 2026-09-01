import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { QwenLocalEmbedder, assertLoopbackEndpoint } from '../src/embed-qwen.js';
import { resolveQualityConfig } from '../src/quality-profile.js';

function nativeVector(seed, dimensions = 2560) {
  return Array.from({ length: dimensions }, (_, index) => ((index + seed) % 17) - 8);
}

async function fakeEmbeddingServer() {
  const requests = [];
  const server = http.createServer(async (request, response) => {
    let body = '';
    for await (const chunk of request) body += chunk;
    const payload = JSON.parse(body || '{}');
    requests.push({
      url: request.url,
      authorization: request.headers.authorization,
      payload
    });
    const inputs = Array.isArray(payload.input) ? payload.input : [payload.input];
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({
      data: inputs.map((_, index) => ({ index, embedding: nativeVector(index + 1) })).reverse()
    }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  return {
    endpoint: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise((resolve) => server.close(resolve))
  };
}

test('Qwen endpoint is strictly loopback HTTP', () => {
  assert.doesNotThrow(() => assertLoopbackEndpoint('http://127.0.0.1:8080'));
  assert.throws(() => assertLoopbackEndpoint('http://localhost:8080'), /loopback/i);
  assert.throws(() => assertLoopbackEndpoint('https://example.com'), /loopback/i);
  assert.throws(() => assertLoopbackEndpoint('http://192.168.1.20:8080'), /loopback/i);
  assert.throws(() => assertLoopbackEndpoint('file:///tmp/socket'), /loopback/i);
});

test('Qwen quality profile binds query instruction and runtime supply-chain identity', () => {
  const resolved = resolveQualityConfig({ embedding: {} }).embedding;
  assert.equal(resolved.queryInstruction, 'Given a web search query, retrieve relevant passages that answer the query');
  assert.equal(resolved.runtimeRevision, 'b10625');
  assert.equal(resolved.runtimeCommit, '0cc5b14959ee3a813bd787baaef50a170493547a');
  assert.equal(resolved.runtimeArchiveSha256, 'f13c74d104c1ff2e37a14ecb2025afe5c9c4c148064badfd8116376018dd5159');
  assert.throws(
    () => resolveQualityConfig({ embedding: { queryInstruction: 'changed default' } }),
    /identity mismatch/i
  );
});

test('Qwen embedder authenticates, preserves order, truncates to 768 and normalizes', async () => {
  const fake = await fakeEmbeddingServer();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'qwen-local-test-'));
  const keyFile = path.join(dir, 'api-key');
  fs.writeFileSync(keyFile, 'test-local-key-32-characters-long!\n', { mode: 0o600 });
  const embedder = new QwenLocalEmbedder({
    endpoint: fake.endpoint,
    apiKeyFile: keyFile,
    dimensions: 768,
    nativeDimensions: 2560,
    batchSize: 2,
    queryInstruction: 'retrieve the relevant passage'
  });
  try {
    const documents = await embedder.embedDocuments(['doc one', 'doc two']);
    assert.equal(documents.length, 2);
    assert.equal(documents[0].length, 768);
    assert.equal(documents[1].length, 768);
    for (const vector of documents) {
      assert.ok(vector.every(Number.isFinite));
      const norm = Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0));
      assert.ok(Math.abs(norm - 1) < 1e-9);
    }
    assert.notDeepEqual(documents[0], documents[1]);
    assert.equal(fake.requests[0].authorization, 'Bearer test-local-key-32-characters-long!');
    assert.deepEqual(fake.requests[0].payload.input, ['doc one', 'doc two']);

    await embedder.embedOne('where is the decision?');
    assert.equal(
      fake.requests[1].payload.input[0],
      'Instruct: retrieve the relevant passage\nQuery:where is the decision?'
    );
  } finally {
    await fake.close();
  }
});

test('Qwen embedder rejects short, non-finite, zero and count-mismatched vectors', async () => {
  const cases = [
    { name: 'short vector', response: { data: [{ index: 0, embedding: [1, 2] }] }, match: /native dimension/i },
    { name: 'non-finite vector', response: { data: [{ index: 0, embedding: [null, ...Array(2559).fill(1)] }] }, match: /finite/i },
    { name: 'zero vector', response: { data: [{ index: 0, embedding: Array(2560).fill(0) }] }, match: /norm/i },
    { name: 'count mismatch', response: { data: [] }, match: /count mismatch/i },
    {
      name: 'invalid indexes',
      response: { data: [{ index: 1, embedding: Array(2560).fill(1) }] },
      match: /indexes/i
    }
  ];
  for (const item of cases) {
    const server = http.createServer((request, response) => {
      request.resume();
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(item.response));
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const endpoint = `http://127.0.0.1:${server.address().port}`;
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'qwen-invalid-test-'));
    const keyFile = path.join(dir, 'api-key');
    fs.writeFileSync(keyFile, 'local-key-32-characters-long-test!\n', { mode: 0o600 });
    const embedder = new QwenLocalEmbedder({ endpoint, apiKeyFile: keyFile });
    try {
      await assert.rejects(() => embedder.embedDocuments(['document']), item.match, item.name);
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }
  }
});
