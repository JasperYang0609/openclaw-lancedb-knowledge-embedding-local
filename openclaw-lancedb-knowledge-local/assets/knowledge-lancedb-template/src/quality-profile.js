const MODEL_SHA256 = '9fd05563211c2d69d74abb8769fa92983a102d11575b2517a119b0037dff217c';

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
    pooling: 'last',
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
