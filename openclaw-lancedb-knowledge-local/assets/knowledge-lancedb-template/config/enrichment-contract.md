# Optional AI enrichment contract (schema v1)

AI enrichment is **auxiliary only**. Never rewrite `project`, `source_type`, `title`, `heading`, `date`, `channel`, paths, chunk IDs, hashes, or source text. Those deterministic fields remain authoritative.

## Privacy gate

`npm run enrich:prepare` writes a local, already-redacted JSONL file. Before sending it to any external model, obtain explicit approval and record who approved it. No command in this template uploads enrichment input automatically.

## Model instruction

For every input JSONL row, return exactly one JSON object on one line. Return no Markdown and no prose. Use this schema:

```json
{
  "id": "copy the input id exactly",
  "doc_type": "decision|sop|bug|risk|status|lesson|reference|mixed|general",
  "tags": ["lowercase", "short", "tags"],
  "importance": 1,
  "summary": "factual summary grounded only in chunk_text",
  "decisions": [],
  "risks": [],
  "action_items": [],
  "confidence": 0.0,
  "model": "provider/model-name",
  "schema_version": 1
}
```

Rules:

- `importance` is an integer from 1 to 5.
- `schema_version` is required and must be the integer `1`; missing, string, fractional, or unsupported versions are rejected.
- `confidence` is from 0 to 1 and must reflect classification certainty, not writing quality.
- Use at most 12 tags and 10 entries in each list.
- Never infer a decision, risk, owner, date, or action that is not stated in `chunk_text`.
- Empty evidence means an empty list.
- Do not include any input `authoritative` fields in the output.

Validate before indexing:

```bash
npm run enrich:validate -- --input data/enrichment/model-output.jsonl
```

Invalid rows are rejected. Rows below `enrichment.minConfidence` are retained only as `low_confidence`, always marked `ai_needs_review=true`, and ignored by reranking. Missing or rejected rows fall back to deterministic metadata; core retrieval remains usable.
