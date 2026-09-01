import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { walkFiles, matchAny } from './glob-lite.js';
import { looksBinary, redactSecrets } from './security.js';
import { chunkMarkdown } from './chunk.js';
import { deriveDeterministicMetadata } from './metadata.js';
import { resolveQualityConfig } from './quality-profile.js';

export function loadConfig() {
  const cfgPath = path.resolve('config/source-map.json');
  return resolveQualityConfig(JSON.parse(fs.readFileSync(cfgPath, 'utf8')));
}

function fileSha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

// More specific patterns score higher: no wildcard (exact relative path) > single-level * / ? > recursive ** wildcard.
function patternSpecificity(pattern) {
  if (!/[*?]/.test(pattern)) return 3;
  if (!pattern.includes('**')) return 2;
  return 1;
}

export function collectFiles(config) {
  const docs = [];
  const skipped = [];
  // First pick, for each file, the source whose include pattern is most specific (ties go to the
  // source listed earlier in config), then read files in a single pass. Attribution stays correct
  // when roots overlap, and each file is indexed exactly once.
  const claims = new Map(); // absPath -> { source, rel, specificity }
  const walkCache = new Map(); // root -> walkFiles result (shared within a run; same-root sources do not rescan)
  for (const source of config.sources) {
    const root = source.root;
    let rootFiles = walkCache.get(root);
    if (!rootFiles) { rootFiles = walkFiles(root); walkCache.set(root, rootFiles); }
    for (const absPath of rootFiles) {
      if (!absPath.toLowerCase().endsWith('.md')) continue;
      const rel = path.relative(root, absPath).split(path.sep).join('/');
      const include = source.include || ['**/*.md'];
      const matchedPatterns = include.filter((p) => matchAny(rel, [p]));
      if (!matchedPatterns.length) continue;
      if (matchAny(rel, source.exclude || [])) continue;
      const specificity = Math.max(...matchedPatterns.map(patternSpecificity));
      const prev = claims.get(absPath);
      if (!prev || specificity > prev.specificity) claims.set(absPath, { source, rel, specificity });
    }
  }
  for (const [absPath, { source, rel }] of claims) {
    let st;
    try { st = fs.statSync(absPath); } catch { continue; }
    if (st.size > (config.maxFileBytes || 1_200_000)) {
      skipped.push({ source: source.id, path: absPath, reason: 'file_too_large', bytes: st.size });
      continue;
    }
    let buf;
    try { buf = fs.readFileSync(absPath); } catch { continue; }
    if (looksBinary(buf)) {
      skipped.push({ source: source.id, path: absPath, reason: 'binary' });
      continue;
    }
    docs.push({ source, absPath, relPath: rel, bytes: st.size, mtimeMs: st.mtimeMs, fileSha256: fileSha256(buf), raw: buf.toString('utf8') });
  }
  return { docs, skipped };
}

export function buildChunks(config) {
  const { docs, skipped } = collectFiles(config);
  const chunks = [];
  const secretHits = [];
  for (const doc of docs) {
    const red = redactSecrets(doc.raw);
    if (red.hits.length) secretHits.push({ path: doc.absPath, hits: red.hits });
    const docChunks = chunkMarkdown({ ...doc, content: red.text, options: config.chunking || {} });
    for (const c of docChunks) {
      chunks.push({
        ...c,
        ...deriveDeterministicMetadata(c),
        file_sha256: doc.fileSha256,
        file_mtime_ms: doc.mtimeMs,
        file_bytes: doc.bytes,
        secret_redactions: red.hits.reduce((a, h) => a + h.count, 0)
      });
    }
  }
  return { docs, chunks, skipped, secretHits };
}
