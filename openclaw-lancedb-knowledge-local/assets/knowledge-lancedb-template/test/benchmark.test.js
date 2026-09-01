import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateBenchmark } from '../src/benchmark.js';

test('benchmark reports hit rate and reciprocal rank from source-grounded expectations', () => {
  const cases = [
    { id: 'q1', query: 'latest VASO status', expected: { project: 'VASO', sourcePathIncludes: 'handoff' } },
    { id: 'q2', query: 'backup procedure', expected: { sourceType: 'ops_doc' } },
    { id: 'q3', query: 'missing document', expected: { project: 'Missing' } }
  ];
  const results = new Map([
    ['q1', [
      { project: 'Other', source_path: '/x/notes.md', source_type: 'project_doc' },
      { project: 'VASO', source_path: '/x/current_handoff.md', source_type: 'project_doc' }
    ]],
    ['q2', [{ project: 'Ops', source_path: '/x/sop.md', source_type: 'ops_doc' }]],
    ['q3', [{ project: 'General', source_path: '/x/general.md', source_type: 'project_doc' }]]
  ]);
  const report = evaluateBenchmark(cases, results, { k: 3 });
  assert.equal(report.total, 3);
  assert.equal(report.hits, 2);
  assert.equal(report.hitRate, 2 / 3);
  assert.ok(Math.abs(report.mrr - 0.5) < 1e-12);
  assert.equal(report.cases[0].rank, 2);
  assert.equal(report.cases[2].rank, null);
});

test('benchmark rejects unknown or empty expectation fields instead of false-passing', () => {
  const results = new Map([['q1', [{ project: 'Anything', source_path: '/x.md' }]]]);
  assert.throws(
    () => evaluateBenchmark([{ id: 'q1', query: 'x', expected: { projet: 'VASO' } }], results),
    /unknown expectation field/i
  );
  assert.throws(
    () => evaluateBenchmark([{ id: 'q1', query: 'x', expected: { project: '' } }], results),
    /non-empty expectation/i
  );
});

test('benchmark requires 20 cases for a release-quality gate by default', () => {
  assert.throws(() => evaluateBenchmark([{ id: 'q1', query: 'x', expected: { project: 'X' } }], new Map(), { releaseGate: true }), /at least 20/i);
});
