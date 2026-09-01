function norm(value) { return String(value ?? '').toLowerCase(); }

const EXPECTATION_FIELDS = new Set([
  'project',
  'sourceType',
  'channel',
  'sourcePathIncludes',
  'titleIncludes',
  'headingIncludes',
  'textIncludes'
]);

function validateExpected(expected) {
  if (!expected || typeof expected !== 'object' || Array.isArray(expected)) throw new Error('Benchmark expected must be an object');
  const keys = Object.keys(expected);
  if (!keys.length) throw new Error('Benchmark expected needs at least one non-empty expectation');
  for (const key of keys) {
    if (!EXPECTATION_FIELDS.has(key)) throw new Error(`Unknown expectation field: ${key}`);
    if (typeof expected[key] !== 'string' || !expected[key].trim()) throw new Error(`Benchmark ${key} must be a non-empty expectation`);
  }
}

function rowMatches(row, expected = {}) {
  if (expected.project && norm(row.project) !== norm(expected.project)) return false;
  if (expected.sourceType && norm(row.source_type) !== norm(expected.sourceType)) return false;
  if (expected.channel && norm(row.channel) !== norm(expected.channel)) return false;
  if (expected.sourcePathIncludes && !norm(row.source_path).includes(norm(expected.sourcePathIncludes))) return false;
  if (expected.titleIncludes && !norm(row.title).includes(norm(expected.titleIncludes))) return false;
  if (expected.headingIncludes && !norm(row.heading).includes(norm(expected.headingIncludes))) return false;
  if (expected.textIncludes && !norm(row.chunk_text).includes(norm(expected.textIncludes))) return false;
  return Object.keys(expected).length > 0;
}

export function evaluateBenchmark(cases, resultsById, { k = 5, releaseGate = false } = {}) {
  if (!Array.isArray(cases) || !cases.length) throw new Error('Benchmark cases are required');
  if (releaseGate && cases.length < 20) throw new Error('Release-quality benchmark requires at least 20 cases');
  const details = [];
  let hits = 0;
  let reciprocalRank = 0;
  for (const item of cases) {
    if (!item?.id || !item?.query || !item?.expected) throw new Error('Each benchmark case needs id, query, and expected');
    validateExpected(item.expected);
    const rows = (resultsById.get(item.id) || []).slice(0, k);
    const idx = rows.findIndex((row) => rowMatches(row, item.expected));
    const rank = idx >= 0 ? idx + 1 : null;
    if (rank) { hits += 1; reciprocalRank += 1 / rank; }
    details.push({ id: item.id, query: item.query, rank, hit: Boolean(rank), expected: item.expected });
  }
  return {
    total: cases.length,
    k,
    hits,
    hitRate: hits / cases.length,
    mrr: reciprocalRank / cases.length,
    cases: details
  };
}

export function benchmarkPasses(report, { minHitRate = 0.8, minMrr = 0.6 } = {}) {
  return report.hitRate >= minHitRate && report.mrr >= minMrr;
}
