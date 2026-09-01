const MODEL_SHA256 = '9fd05563211c2d69d74abb8769fa92983a102d11575b2517a119b0037dff217c';
const RUNTIME_ARCHIVE_SHA256 = 'f13c74d104c1ff2e37a14ecb2025afe5c9c4c148064badfd8116376018dd5159';
const QUERY_INSTRUCTION = 'Given a web search query, retrieve relevant passages that answer the query';

export function resolveQualityConfig(config = {}) {
  const embedding = { ...(config.embedding || {}) };
  if ((embedding.provider || 'qwen-local') !== 'qwen-local') {
    throw new Error(`Only qwen-local embedding is supported; got ${embedding.provider}`);
  }
  const required = {
    provider: 'qwen-local',
    model: 'Qwen3-Embedding-4B-Q5_K_M',
    dimensions: 768,
    nativeDimensions: 2560,
    quantization: 'Q5_K_M',
    modelSha256: MODEL_SHA256,
    runtimeRevision: 'b10625',
    runtimeCommit: '0cc5b14959ee3a813bd787baaef50a170493547a',
    runtimeArchiveSha256: RUNTIME_ARCHIVE_SHA256,
    pooling: 'last',
    queryInstruction: QUERY_INSTRUCTION,
    normalization: 'truncate-768-then-l2'
  };
  for (const [key, value] of Object.entries(required)) {
    if (embedding[key] !== undefined && embedding[key] !== value) {
      throw new Error(`Qwen embedding identity mismatch for ${key}`);
    }
    embedding[key] = value;
  }
  return { ...config, embedding };
}
