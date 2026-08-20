# OpenClaw LanceDB Architecture

## Universal LLM readability

This skill may be consumed by OpenClaw, Claude Opus, Codex, Cursor, or another client-side agent. Keep all reusable instructions model-agnostic:

- Explain intent before commands.
- Use stable file names and obvious configuration keys.
- Avoid relying on hidden memory, private channel conventions, or model-specific tool names.
- When an action depends on OpenClaw, label it as such and include the underlying shell/project command when safe.
- Require source citation in answers so any LLM can ground its output in retrieved paths.

## Purpose

Create a local semantic retrieval layer for OpenClaw so old decisions, project progress, backup summaries, and handoff notes can be found after session reset or context loss.

## Proven Ansai setup

- Project: `knowledge-lancedb`
- DB: local LanceDB file store at `data/lancedb`
- Table: `knowledge_chunks`
- Primary command: `node src/cli.js`
- Commands: `scan`, `index`, `incremental`, `sync-state`, `status`, `audit`, `search`, `compact-cache`, `prepare-enrichment`, `validate-enrichment`, `benchmark`, `profile`
- Default source types: `memory`, `backup_summary`, `project_doc`, `ops_doc`; `discord_raw` is an explicit opt-in
- Production embedding used by Ansai after Jasper approval: Google Gemini `gemini-embedding-001`, 768 dimensions
- Safe default for new clients: `local-hash-v1`, 384 dimensions, no external API calls

## Data flow

1. `config/source-map.json` declares roots, includes, excludes, project labels, and source types. A source's `project` label is authoritative for every file it claims; filename heuristics are only a fallback for sources without a label, and they match the relative path only.
2. Files are collected from markdown-only sources, skipping binaries and large files. Directory walks skip `node_modules`, `.git`, build output, `data`, `output`, and `.lancedb`, and each root is scanned once per run even when several sources share it. When multiple sources match the same file, the source with the most specific include pattern wins (exact relative path > single-level wildcard > recursive `**`), with config order breaking ties, so each file is indexed exactly once with correct attribution.
3. Text is redacted by `src/security.js` before chunking/embedding.
4. Markdown is split by headings and paragraph boundaries; only a single oversized paragraph falls back to sentence/whitespace-aware hard splitting with overlap.
5. Deterministic rules attach a stable document type, tags, importance, and metadata schema version. These fields are authoritative and do not call an LLM.
6. Optional AI output is read from a validated local JSONL cache. It is written only to `ai_*` columns, gets a derived confidence/review state, and cannot overwrite source metadata. Disabled, missing, invalid, or low-confidence enrichment leaves deterministic retrieval intact.
7. Rows are embedded and written to LanceDB. Remote embedding vectors are L2-normalized before they are written and before queries (zero vectors are left unchanged); the JSONL embedding cache stores the raw API vectors. Append mode deletes existing rows for the same `source_path` before adding, so re-indexing a file never duplicates chunks.
8. Incremental indexing compares `source_path + file_sha256` plus a build fingerprint for schema, embedding identity, chunking, and enrichment input. A schema or vector-dimension migration triggers a one-time full rebuild instead of adding incompatible rows.
9. `sync-state` writes schema-v2 state only after validating the existing table schema, vector dimensions, embedding identity, complete chunk IDs/hashes, deterministic metadata, and enrichment fields against current sources. Legacy or mismatched tables are rejected with a full-index instruction.
10. Search embeds the query, fetches vector candidates, then reranks with keyword overlap (including deterministic metadata and only `valid` AI auxiliary text), recency, and progress-document boosts.
11. Benchmarking measures source-grounded Hit@K and MRR. A release-quality gate requires at least 20 reviewed cases and rejects unknown or empty expectation fields.

## Row schema

Each chunk row stores:

- `id`: stable hash of path, chunk index, and content hash
- `source_id`, `source_path`, `rel_path`, `source_type`
- `project`, `channel`, `title`, `heading`, `date`
- `chunk_index`, `chunk_text`, `token_estimate`
- `file_sha256`, `file_mtime_ms`, `file_bytes`, `content_sha256`
- `secret_redactions`
- `deterministic_doc_type`, `deterministic_tags_json`, `deterministic_importance`, `deterministic_metadata_version`
- auxiliary `ai_doc_type`, `ai_tags_json`, `ai_importance`, `ai_summary`, decisions/risks/actions JSON, `ai_confidence`, `ai_needs_review`, `ai_enrichment_status`, model, and schema version
- `embedding_provider`, `embedding_model`, `embedding_dimensions`
- `vector`

## Embedding modes

### local-hash-v1

Use for proof-of-concept, private data, and installs before approval. It is deterministic and offline, but retrieval quality is weaker than production embeddings.

### google-gemini

Use after explicit privacy approval. It supports multilingual retrieval better and matches the Ansai formal setup:

```json
{
  "provider": "google-gemini",
  "model": "gemini-embedding-001",
  "profile": "balanced",
  "dimensions": 768,
  "documentTaskType": "RETRIEVAL_DOCUMENT",
  "queryTaskType": "RETRIEVAL_QUERY",
  "batchSize": 40,
  "throttleMs": 250,
  "cachePath": "./data/embedding-cache/google-gemini-embedding-001-768.jsonl"
}
```

`balanced` is the stable default at 768 dimensions. `high-quality` uses 3072 dimensions and a separate cache path. Every Gemini cache path is normalized to include its vector dimension, including legacy unsuffixed custom paths. A profile change is an opt-in migration: back up the DB/cache, rebuild the table, and compare a reviewed benchmark before adopting it. A larger vector is not automatically better for every corpus.

## AI enrichment isolation

The template never calls an LLM for enrichment. `prepare-enrichment` creates a local redacted JSONL export; an explicitly approved model workflow may transform it using `config/enrichment-contract.md`; `validate-enrichment` enforces the fixed schema. Core fields are forbidden in output. Confidence below the configured threshold forces `ai_needs_review=true`, and malformed/missing rows use schema-stable fallback values.

This separation keeps the public skill model-agnostic: GPT, Claude, Gemini, a local model, or a human process can produce the same contract without changing index code. AI classification is never the only retrieval route.

API key resolution order in the template:

1. `GOOGLE_API_KEY`
2. `GEMINI_API_KEY`
3. `OPENCLAW_CONFIG_PATH`
4. `~/.openclaw/openclaw.json`
5. `~/.openclaw/config.json`

## Incremental indexing

Use `npm run incremental` after backup jobs. The wrapper script creates a lock directory at `data/index.lock` to prevent overlapping runs and writes logs under `reports/cron-logs/`. After each run the wrapper compacts the embedding cache when it exceeds 200MB (`npm run compact-cache`) and rotates timestamped report manifests and cron logs, keeping the 14 most recent of each; `*.latest.json` files are always kept.

If the table or state file is missing, the row schema is legacy, or vector dimensions changed, incremental falls back to a full index.

A partial `index --project NAME --append` or `index --limit N --append` run merges only the touched paths into `data/index-state.json`; overwrite runs rewrite the state in full to mirror the rebuilt table.

## Coverage audit and restore snapshots

`npm run audit` rebuilds the expected chunks from current sources, validates every row's content hash, deterministic metadata/tags, optional AI fields, embedding provider/model/dimensions, and reports row counts by `source_type`. It is read-only and fails when sources and table diverge.

`npm run snapshot:backup -- --backup-root <PATH>` creates a checksummed restore set containing the LanceDB table, index state, embedding cache, validated enrichment when present, source-map config, deterministic tag/security rules, package lock, and latest manifests. The snapshot is staged and verified before publication. Existing snapshot names are never overwritten; choose a new `--snapshot-name` for another run on the same date.

## Embedding cache maintenance

`npm run compact-cache` rewrites the JSONL embedding cache, keeping only vectors for chunks the current sources still produce plus query vectors that match the current model/dimensions. It never calls the embedding API, so it is safe to run at any time. The cache key is derived from the same `project\ntitle\nheading\nchunk_text` embedding input used at index time.

## Search behavior

Use:

```bash
npm run search -- "query" -- --project ProjectName --limit 5
```

The template fetches more vector candidates than needed, then reranks with:

- vector similarity
- Chinese/English keyword overlap
- recency for progress/status queries
- boosts for handoff/current/progress files
- deterministic tags and validated AI summaries/tags for keyword overlap

## Benchmark gate

Copy `config/benchmark.example.json` to `config/benchmark.json` and replace all examples with reviewed questions and expected source fields from the target corpus. Run `npm run benchmark -- --file config/benchmark.json --release-gate`. The default threshold is Hit@5 >= 0.80 and MRR >= 0.60, but deployments may set stricter values. Store the JSON report with the migration record and compare old/new profiles before changing production.

## OpenClaw answer rule

When answering historical or project-state questions, search first, then cite source paths. Do not present retrieved content as guaranteed latest truth if the source is old; state the source date/path and recommend checking live state when needed.
