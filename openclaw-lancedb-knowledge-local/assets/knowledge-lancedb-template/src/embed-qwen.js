import fs from 'node:fs';
import path from 'node:path';

const LOOPBACK_HOSTS = new Set(['127.0.0.1', '::1', '[::1]']);
const DEFAULT_QUERY_INSTRUCTION = 'Given a web search query, retrieve relevant passages that answer the query';

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export function assertLoopbackEndpoint(endpoint) {
  let url;
  try { url = new URL(endpoint); }
  catch { throw new Error('Qwen endpoint must be a valid loopback HTTP URL'); }
  if (url.protocol !== 'http:' || !LOOPBACK_HOSTS.has(url.hostname)) {
    throw new Error('Qwen endpoint must use loopback HTTP; cloud and LAN endpoints are forbidden');
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error('Qwen loopback endpoint must not contain credentials, query parameters, or fragments');
  }
  return url;
}

function readApiKey(apiKeyFile) {
  if (!apiKeyFile) throw new Error('Qwen apiKeyFile is required');
  const resolved = path.resolve(apiKeyFile);
  if (fs.lstatSync(resolved).isSymbolicLink()) throw new Error('Qwen apiKeyFile must not be a symbolic link');
  const stat = fs.statSync(resolved);
  if (!stat.isFile()) throw new Error('Qwen apiKeyFile must be a regular file');
  if (process.platform !== 'win32' && (stat.mode & 0o077) !== 0) {
    throw new Error('Qwen apiKeyFile permissions must deny group and other access');
  }
  const key = fs.readFileSync(resolved, 'utf8').trim();
  if (key.length < 32 || key.length > 512 || /\s/.test(key)) {
    throw new Error('Qwen apiKeyFile contains an invalid local credential');
  }
  return key;
}

export function truncateAndNormalize(vector, dimensions, nativeDimensions) {
  if (!Array.isArray(vector) || vector.length !== nativeDimensions) {
    throw new Error(`Unexpected Qwen native dimension: ${vector?.length}; expected ${nativeDimensions}`);
  }
  const truncated = vector.slice(0, dimensions);
  if (!truncated.every(Number.isFinite)) throw new Error('Qwen embedding contains non-finite values');
  let squared = 0;
  for (const value of truncated) squared += value * value;
  const norm = Math.sqrt(squared);
  if (!Number.isFinite(norm) || norm === 0) throw new Error('Qwen embedding norm must be finite and non-zero');
  return truncated.map((value) => value / norm);
}

export class QwenLocalEmbedder {
  constructor(config = {}) {
    this.endpoint = assertLoopbackEndpoint(config.endpoint || 'http://127.0.0.1:8080');
    this.apiKey = readApiKey(config.apiKeyFile);
    this.model = config.model || 'Qwen3-Embedding-4B-Q5_K_M';
    this.dimensions = Number(config.dimensions || 768);
    this.nativeDimensions = Number(config.nativeDimensions || 2560);
    this.batchSize = Number(config.batchSize || 4);
    this.maxInputChars = Number(config.maxInputChars || 12_000);
    this.timeoutMs = Number(config.timeoutMs || 120_000);
    this.maxRetries = Number(config.maxRetries ?? 3);
    this.queryInstruction = config.queryInstruction || DEFAULT_QUERY_INSTRUCTION;
    if (!Number.isInteger(this.dimensions) || this.dimensions < 32 || this.dimensions > this.nativeDimensions) {
      throw new Error('Qwen dimensions must be an integer from 32 through nativeDimensions');
    }
    if (!Number.isInteger(this.nativeDimensions) || this.nativeDimensions < this.dimensions) {
      throw new Error('Qwen nativeDimensions must be an integer not smaller than dimensions');
    }
    if (!Number.isInteger(this.batchSize) || this.batchSize < 1 || this.batchSize > 32) {
      throw new Error('Qwen batchSize must be an integer from 1 through 32');
    }
  }

  validateInputs(inputs) {
    if (!Array.isArray(inputs) || inputs.length < 1 || inputs.length > this.batchSize) {
      throw new Error(`Qwen request must contain 1 through ${this.batchSize} inputs`);
    }
    for (const input of inputs) {
      if (typeof input !== 'string' || input.length < 1 || input.length > this.maxInputChars) {
        throw new Error(`Qwen input must be a non-empty string no longer than ${this.maxInputChars} characters`);
      }
    }
  }

  async request(inputs) {
    this.validateInputs(inputs);
    const endpoint = new URL('/v1/embeddings', this.endpoint);
    for (let attempt = 0; attempt <= this.maxRetries; attempt += 1) {
      let response;
      try {
        response = await fetch(endpoint, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${this.apiKey}`,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ input: inputs, encoding_format: 'float' }),
          signal: AbortSignal.timeout(this.timeoutMs)
        });
      } catch (error) {
        if (attempt === this.maxRetries) throw new Error(`Qwen sidecar request failed after retries: ${error.name}`);
        await sleep(Math.min(5_000, 250 * (2 ** attempt)));
        continue;
      }
      const body = await response.text();
      if (!response.ok) {
        const retryable = response.status === 429 || response.status >= 500;
        if (retryable && attempt < this.maxRetries) {
          await sleep(Math.min(5_000, 250 * (2 ** attempt)));
          continue;
        }
        throw new Error(`Qwen sidecar returned HTTP ${response.status}`);
      }
      let parsed;
      try { parsed = JSON.parse(body); }
      catch { throw new Error('Qwen sidecar returned invalid JSON'); }
      const rows = Array.isArray(parsed.data) ? [...parsed.data].sort((a, b) => a.index - b.index) : [];
      if (rows.length !== inputs.length) {
        throw new Error(`Qwen embedding count mismatch: got ${rows.length}, expected ${inputs.length}`);
      }
      if (rows.some((row, index) => row.index !== index)) {
        throw new Error('Qwen embedding indexes are missing, duplicated, or out of range');
      }
      return rows.map((row) => truncateAndNormalize(row.embedding, this.dimensions, this.nativeDimensions));
    }
    throw new Error('Qwen sidecar request failed');
  }

  async embedDocuments(texts, onProgress = () => {}) {
    const output = [];
    for (let start = 0; start < texts.length; start += this.batchSize) {
      const batch = texts.slice(start, start + this.batchSize);
      output.push(...await this.request(batch));
      onProgress({ phase: 'local', done: output.length, total: texts.length, batchSize: batch.length });
    }
    return output;
  }

  async embedOne(text) {
    const query = `Instruct: ${this.queryInstruction}\nQuery:${text}`;
    const [vector] = await this.request([query]);
    return vector;
  }
}

export function getQwenEmbedder(config = {}) {
  if (config.provider !== 'qwen-local') throw new Error(`Unsupported Qwen provider: ${config.provider}`);
  return new QwenLocalEmbedder(config);
}
