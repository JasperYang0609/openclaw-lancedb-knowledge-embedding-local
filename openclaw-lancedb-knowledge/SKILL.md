---
name: openclaw-lancedb-knowledge
description: Build, operate, troubleshoot, or replicate Jasper/Ansai-style OpenClaw knowledge search using local LanceDB, Markdown source maps, secret redaction, incremental indexing, Google Gemini or local-hash embeddings, and source-cited retrieval for project memory, Discord/channel backup summaries, Obsidian-style vaults, handoff files, and client documentation. Use when asked to install or create an OpenClaw LanceDB knowledge base, search historical project context, index OpenClaw memory/backups, configure embeddings, run incremental indexing/cron, or package this workflow for a client.
---

# OpenClaw LanceDB Knowledge

Use this skill to create and operate an OpenClaw-first semantic knowledge layer. Write and preserve instructions so they are readable by general LLM agents, especially Claude Opus-class client agents, not only by this specific OpenClaw instance: use plain imperative steps, explicit paths/commands, minimal local jargon, and source-cited output rules.

```
OpenClaw memory / backup summaries / project docs / Obsidian-style markdown
  -> source-map filters
  -> secret redaction
  -> paragraph-aware markdown heading chunks
  -> authoritative deterministic metadata
  -> optional validated AI auxiliary metadata
  -> embeddings
  -> local LanceDB table
  -> benchmarked, source-cited search answers
```

## LLM compatibility rule

Design every client-facing instruction so a general LLM can follow it without knowing this workspace's private habits:

- Use plain English operational steps; avoid unexplained Ansai-only shorthand.
- Keep commands copy-pasteable and parameterized (`--workspace`, `--backup-root`, `--project-root`) instead of hard-coding Jasper's machine paths.
- State privacy gates explicitly before external embeddings.
- Prefer deterministic scripts for fragile steps, but document the script's purpose in natural language.
- Assume many clients will use Claude Opus or another capable LLM to read the skill; make the workflow model-agnostic and source-grounded.
- Keep OpenClaw-specific behavior named as OpenClaw-specific, and provide a generic fallback when possible.

## Default workflow

1. **Confirm scope and privacy.** Identify the workspace, backup summary root, and project-doc roots. If using external embeddings, get explicit approval because redacted private chunks leave the machine.
2. **Bootstrap or inspect the project.** Use `scripts/bootstrap_openclaw_lancedb.py` for a new install, or inspect an existing `knowledge-lancedb/` folder.
3. **Edit `config/source-map.json`.** Prefer summary/project/handoff markdown first. Use `--include-discord-raw` when complete Discord message retrieval is an explicit requirement and privacy/size have been reviewed.
4. **Choose a quality profile.** Keep Gemini at `balanced`/768 unless a benchmark justifies the opt-in 3072-dimensional `high-quality` profile. Changing dimensions requires a full rebuild.
5. **Run gates.** Run `npm ci --ignore-scripts`, `npm test`, `npm run scan`, then `npm run index` or `npm run incremental`.
6. **Benchmark retrieval.** Maintain 20–50 source-grounded queries and run the release gate before adopting a quality change.
7. **Use AI enrichment only as an optional second layer.** Deterministic fields remain authoritative; validate JSONL output and review low-confidence rows.
8. **Search before answering historical/project-state questions.** Use project filters when possible and cite source paths in the answer.
9. **Set daily incremental indexing** after OpenClaw backup jobs, using the bundled `knowledge_index_incremental.sh` wrapper or the platform's cron mechanism.
10. **Back up the restore set.** Run `npm run snapshot:backup -- --backup-root <PATH>` so the LanceDB table, index state, embedding cache, deterministic tag rules, optional AI enrichment, and restore config are checksummed together.
11. **Close out in order.** Create the final snapshot only after Raw backup, Core Backup, and LanceDB indexing closeouts. Pass every timestamp with `--require-after`, then require checksum verification, isolated restore, database open, row-count readback, and retention before reporting snapshot PASS.

## Bootstrap command

From the installed skill folder:

```bash
python3 scripts/bootstrap_openclaw_lancedb.py \
  --target ~/.openclaw/workspace/knowledge-lancedb \
  --workspace ~/.openclaw/workspace \
  --backup-root "$HOME/Desktop/<伺服器名稱>備份/頻道紀錄" \
  --project-root "$HOME/Desktop/Client_Project" \
  --project-name ClientProject \
  --include-discord-raw \
  --npm-install
```

Omit `--include-discord-raw` when summaries are sufficient. Raw Discord indexing is opt-in because it increases corpus size and may contain sensitive material; secret redaction still applies before embedding.

`--npm-install` uses a fixed `npm ci --ignore-scripts` command and never invokes a shell. Only add `--allow-package-scripts` after reviewing the lockfile and dependency lifecycle scripts; that flag requires `--npm-install`. Run `npm test` explicitly before `npm run postrun:check` because the post-run check is intentionally non-executing.

Use local-only embeddings by default. To enable Google Gemini embeddings:

```bash
python3 scripts/bootstrap_openclaw_lancedb.py \
  --google-gemini \
  --embedding-profile balanced \
  --approved-by "Client approved external embeddings on YYYY-MM-DD" \
  --overwrite
```

For an opt-in 3072-dimensional rebuild, use `--embedding-profile high-quality`. Do not switch an existing index in place: back it up, update config, run a full `npm run index`, then pass the benchmark gate.

## Commands after bootstrap

```bash
cd ~/.openclaw/workspace/knowledge-lancedb
npm run scan
npm run index
npm run status
npm run audit
npm run search -- "VASO 文件中心做到哪" -- --project VASO --limit 5
npm run incremental
npm run profile
npm run snapshot:backup -- \
  --backup-root "$HOME/Desktop/<伺服器名稱>備份/LanceDB知識庫備份" \
  --snapshot-name "daily-$(date +%F)" \
  --retention-days 30 \
  --transient-retention-days 7 --transient-max-count 10 \
  --restore-canary --verify-db
npm run postrun:check
```

`npm run audit` validates current source chunks against every indexed row, including deterministic metadata/tag fields and embedding identity. A mismatch fails closed with a full-index instruction.

The snapshot command writes `snapshots/<YYYY-MM-DD>/snapshot-manifest.json` and `CHECKSUMS.sha256`. Verify a copied snapshot with:

```bash
python3 scripts/snapshot_knowledge_assets.py \
  --verify-snapshot "$HOME/Desktop/<伺服器名稱>備份/LanceDB知識庫備份/snapshots/<YYYY-MM-DD>"
```

For daily automation, use `daily-YYYY-MM-DD` names with `--retention-days 30`. Use `--transient-retention-days 7 --transient-max-count 10` for the combined `incident-*` and `repair-*` set. Retention runs only after successful verification. A transient snapshot containing `.keep` is protected; unrelated manual snapshots are ignored.

Cron verification must pass an absolute `--verify-snapshot` path. Add `--expected-snapshot-root` to prevent verification against the project directory or another backup root. Never verify from only a snapshot name or ambiguous relative path.

For a final closeout, pass the Raw, Core Backup, and LanceDB completion timestamps as repeated `--require-after` values. Also pass `--restore-canary --verify-db --expected-row-count <AUDITED_ROWS>`. Snapshot PASS requires freshness, manifest/checksum, isolated restore, database open, row-count equality, and both retention policies.

Keep customer reports compact: include snapshot path, file/byte counts, manifest SHA-256, gate results, and evidence paths. Never inline the manifest's per-file `assets` array into a chat/report; the manifest remains on disk for audit.

Before enabling the cron, audit `openclaw cron list --all --json` with `scripts/audit_cron_tooling.py`. Any `payload.toolsAllow` field is invalid, including an empty array. Remove it with `openclaw cron edit <job-id> --clear-tools`, then run and delete a temporary isolated GPT/Codex canary that prints `TOOL_OK` from `pwd && echo TOOL_OK`.

Use `npm run search -- "query" -- --real-date-summary-only` for summary validation. This mode accepts only `summary/YYYY-MM-DD.md` backup rows and rejects `_inventory-index*` synthetic coverage files.

## Optional AI enrichment

AI enrichment may add `ai_tags_json`, `ai_doc_type`, `ai_importance`, `ai_summary`, `ai_decisions_json`, `ai_risks_json`, and `ai_action_items_json`. It may never replace `project`, `source_type`, paths, title, heading, date, channel, hashes, chunk IDs, or source text.

```bash
# Local only: creates redacted JSONL; it does not call a model.
npm run enrich:prepare -- --output data/enrichment/input.jsonl

# Process input.jsonl with any approved model using config/enrichment-contract.md,
# then validate the one-object-per-line output.
npm run enrich:validate -- --input data/enrichment/model-output.jsonl
```

Only after validation, set `enrichment.enabled=true` and point `enrichment.inputPath` at the validated file. The validator requires integer `schema_version: 1`. Rows below `minConfidence` are stored as `low_confidence` with `ai_needs_review=true` and are excluded from reranking. Missing or invalid output falls back to deterministic metadata, so an AI failure cannot remove the core index.

## Retrieval benchmark gate

Copy and customize the 20-case scaffold:

```bash
cp config/benchmark.example.json config/benchmark.json
npm run benchmark -- --file config/benchmark.json --release-gate
```

The gate reports Hit@K and mean reciprocal rank (MRR). Expectations are matched against source-grounded fields such as project, source type, path, title, heading, or chunk text. Do not publish a quality claim from the generic example; replace every case with real, reviewed ground truth for that corpus.

## Answer pattern after retrieval

For user-facing answers, keep it short and source-grounded:

- Conclusion / current state
- Evidence from top retrieved chunks
- Source path(s)
- Recommended next step

If retrieval is weak, say so and run a narrower search with project/channel/date terms before concluding.

## Safety rules

- Do not index secrets intentionally. Keep `**/*secret*`, `**/*token*`, `.env`, `.git`, `node_modules`, builds, and media out by default.
- Secret redaction is a guardrail, not permission to ingest credentials.
- External embedding providers require explicit approval per client/project.
- Record Discord raw policy as `NOT_CONFIRMED`, `APPROVED_EXTERNAL`, or `LOCAL_ONLY`. When raw is not approved/configured, exact message lookup must remain `SKIPPED_PRIVACY_GATE`; this is an expected partial privacy result, not a system failure.
- Sending enrichment input to an external LLM requires a separate explicit approval; preparing or validating local JSONL does not upload it.
- AI enrichment is auxiliary and confidence-gated. Never use an AI tag as the only retrieval path or as authority over deterministic metadata.
- Changing embedding model or dimensions requires full reindex.
- Back up the LanceDB directory and embedding cache before a profile migration; 768- and 3072-dimensional vectors use separate caches and table schemas.
- Keep metadata/tag rules and validated enrichment beside the database backup. Copying `data/lancedb` alone is insufficient for a reproducible restore.
- Do not index raw Discord/chat backups first unless summaries are insufficient; raw data is noisy and privacy-heavy.

## Memory index health and provider migration

Use this section when OpenClaw memory/vector search reports `index metadata is missing`, `vector search paused`, `Dirty: yes`, `Index identity: missing`, or `0 chunks` after files exist.

Diagnosis:
1. Run the platform memory index status command for the affected agent, e.g. `openclaw memory status --index --agent main` when available.
2. Identify the configured embedding provider/model and dimensions.
3. Check whether the index was created with a different provider/model or has missing metadata.
4. Treat provider/model/dimension changes as a migration requiring full reindex.

Repair:
1. Confirm privacy before using external embeddings; private chunks may leave the machine.
2. Fix the desired embedding provider/model in config or project docs.
3. Force rebuild the index, e.g. `openclaw memory index --force --agent main` when available.
4. Verify status shows indexed files/chunks and no paused vector search.
5. Run sample searches for: people/permissions, project decisions, and recent tasks.

Prevention:
- Do not silently switch embedding provider/model on client deployments.
- Record provider/model choice in the client handoff.
- Add daily or weekly index status checks for client knowledge bases.
- If status is broken, prefer repair/reindex before answering memory-dependent questions.

## Post-run self-check

After changing the template, indexing scripts, source-map defaults, or redaction logic, run this inside the generated `knowledge-lancedb/` project:

```bash
npm run postrun:check
```

The check validates the template command surface, source-map safety defaults, incremental wrapper locking/report rotation, and the bundled test suite. It is intentionally local-only and does not index private workspace content.

## Bundled resources

- `scripts/bootstrap_openclaw_lancedb.py` — creates a portable `knowledge-lancedb` project from the bundled template.
- `assets/knowledge-lancedb-template/` — Node + LanceDB CLI template copied by the bootstrap script.
- `references/architecture.md` — detailed architecture, schema, ranking, and operations notes.
- `references/source-map-patterns.md` — source-map examples for OpenClaw memory, backup summaries, Obsidian vaults, project docs, and code docs.
