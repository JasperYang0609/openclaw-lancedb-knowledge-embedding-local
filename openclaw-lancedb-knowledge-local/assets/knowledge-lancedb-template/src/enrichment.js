import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const DOC_TYPES = new Set(['decision', 'sop', 'bug', 'risk', 'status', 'lesson', 'reference', 'mixed', 'general']);
const CORE_FIELDS = new Set([
  'source_id', 'source_path', 'rel_path', 'source_type', 'project', 'channel', 'title', 'heading',
  'date', 'chunk_index', 'chunk_text', 'file_sha256', 'content_sha256'
]);
const ALLOWED_FIELDS = new Set([
  'id', 'doc_type', 'tags', 'importance', 'summary', 'decisions', 'risks', 'action_items',
  'confidence', 'model', 'schema_version', 'needs_review'
]);

function cleanText(value, max) {
  return String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, max);
}

function cleanList(value, { maxItems = 10, maxChars = 240 } = {}) {
  if (!Array.isArray(value)) return null;
  const out = [];
  const seen = new Set();
  for (const item of value) {
    const text = cleanText(item, maxChars);
    if (!text) continue;
    const key = text.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(text);
    if (out.length >= maxItems) break;
  }
  return out;
}

export function validateEnrichmentRecord(record, { minConfidence = 0.75 } = {}) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return { ok: false, error: 'record must be an object' };
  const forbidden = Object.keys(record).filter((key) => CORE_FIELDS.has(key));
  if (forbidden.length) return { ok: false, error: `authoritative fields are forbidden: ${forbidden.join(', ')}` };
  const unknown = Object.keys(record).filter((key) => !ALLOWED_FIELDS.has(key));
  if (unknown.length) return { ok: false, error: `unknown fields: ${unknown.join(', ')}` };
  if (!Number.isInteger(record.schema_version) || record.schema_version !== 1) {
    return { ok: false, error: 'schema_version must be the supported integer value 1' };
  }
  const id = cleanText(record.id, 128);
  if (!id) return { ok: false, error: 'id is required' };
  const docType = cleanText(record.doc_type, 40).toLowerCase();
  if (!DOC_TYPES.has(docType)) return { ok: false, error: `invalid doc_type: ${docType || '(empty)'}` };
  const tagsRaw = cleanList(record.tags, { maxItems: 12, maxChars: 48 });
  if (!tagsRaw) return { ok: false, error: 'tags must be an array' };
  const tags = [];
  const seenTags = new Set();
  for (const value of tagsRaw) {
    const tag = value.toLowerCase();
    if (!seenTags.has(tag)) { seenTags.add(tag); tags.push(tag); }
  }
  const importance = Number(record.importance);
  if (!Number.isInteger(importance) || importance < 1 || importance > 5) return { ok: false, error: 'importance must be an integer from 1 to 5' };
  const confidence = Number(record.confidence);
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) return { ok: false, error: 'confidence must be a number from 0 to 1' };
  const summary = cleanText(record.summary, 600);
  if (!summary) return { ok: false, error: 'summary is required' };
  const decisions = cleanList(record.decisions);
  const risks = cleanList(record.risks);
  const actionItems = cleanList(record.action_items);
  if (!decisions || !risks || !actionItems) return { ok: false, error: 'decisions, risks, and action_items must be arrays' };
  return {
    ok: true,
    value: {
      id,
      doc_type: docType,
      tags,
      importance,
      summary,
      decisions,
      risks,
      action_items: actionItems,
      confidence,
      model: cleanText(record.model, 120),
      schema_version: 1,
      needs_review: confidence < minConfidence
    }
  };
}

function emptyFields(status, needsReview) {
  return {
    ai_doc_type: '',
    ai_tags_json: '[]',
    ai_importance: 0,
    ai_summary: '',
    ai_decisions_json: '[]',
    ai_risks_json: '[]',
    ai_action_items_json: '[]',
    ai_confidence: 0,
    ai_needs_review: needsReview,
    ai_enrichment_status: status,
    ai_enrichment_model: '',
    ai_enrichment_schema_version: 1
  };
}

export function applyAuxiliaryEnrichment(chunk, enrichment, { enabled = true } = {}) {
  if (!enrichment) return { ...chunk, ...emptyFields(enabled ? 'missing' : 'disabled', enabled) };
  return {
    ...chunk,
    ai_doc_type: enrichment.doc_type,
    ai_tags_json: JSON.stringify(enrichment.tags),
    ai_importance: enrichment.importance,
    ai_summary: enrichment.summary,
    ai_decisions_json: JSON.stringify(enrichment.decisions),
    ai_risks_json: JSON.stringify(enrichment.risks),
    ai_action_items_json: JSON.stringify(enrichment.action_items),
    ai_confidence: enrichment.confidence,
    ai_needs_review: Boolean(enrichment.needs_review),
    ai_enrichment_status: enrichment.needs_review ? 'low_confidence' : 'valid',
    ai_enrichment_model: enrichment.model || '',
    ai_enrichment_schema_version: enrichment.schema_version || 1
  };
}

export function loadEnrichmentCache(config = {}) {
  if (!config.enabled) return { enabled: false, records: new Map(), stats: { valid: 0, invalid: 0 } };
  if (!config.privacyApprovedAt || !config.privacyApprovedBy) {
    throw new Error('AI enrichment is enabled but explicit privacy approval is missing (privacyApprovedAt / privacyApprovedBy)');
  }
  const inputPath = path.resolve(config.inputPath || './data/enrichment/validated.jsonl');
  if (!fs.existsSync(inputPath)) return { enabled: true, inputPath, records: new Map(), stats: { valid: 0, invalid: 0, missingFile: true } };
  const records = new Map();
  let valid = 0;
  let invalid = 0;
  for (const line of fs.readFileSync(inputPath, 'utf8').split(/\r?\n/).filter(Boolean)) {
    let parsed;
    try { parsed = JSON.parse(line); } catch { invalid += 1; continue; }
    const checked = validateEnrichmentRecord(parsed, config);
    if (!checked.ok) { invalid += 1; continue; }
    records.set(checked.value.id, checked.value);
    valid += 1;
  }
  const sha256 = crypto.createHash('sha256').update(fs.readFileSync(inputPath)).digest('hex');
  return { enabled: true, inputPath, records, sha256, stats: { valid, invalid } };
}

export function validateEnrichmentJsonl(inputPath, outputPath, options = {}) {
  const lines = fs.readFileSync(path.resolve(inputPath), 'utf8').split(/\r?\n/).filter(Boolean);
  const valid = [];
  const errors = [];
  lines.forEach((line, index) => {
    let parsed;
    try { parsed = JSON.parse(line); } catch (err) { errors.push({ line: index + 1, error: `invalid JSON: ${err.message}` }); return; }
    const checked = validateEnrichmentRecord(parsed, options);
    if (!checked.ok) errors.push({ line: index + 1, id: parsed?.id || null, error: checked.error });
    else valid.push(checked.value);
  });
  const resolved = path.resolve(outputPath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  fs.writeFileSync(resolved, valid.map((row) => JSON.stringify(row) + '\n').join(''));
  return { input: path.resolve(inputPath), output: resolved, total: lines.length, valid: valid.length, invalid: errors.length, errors };
}
