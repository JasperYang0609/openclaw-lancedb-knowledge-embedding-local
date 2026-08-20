# OpenClaw LanceDB Knowledge Skill

OpenClaw skill for building a local LanceDB semantic knowledge layer over OpenClaw memory, Discord/channel backup summaries or opt-in raw history, Obsidian-style markdown vaults, handoff files, and client project docs. The skill is written to be readable by general LLM agents, including Claude Opus-class client agents, with explicit commands, privacy gates, and source-cited answer rules.

## Install

```bash
npx skills add JasperYang0609/openclaw-lancedb-knowledge-skill@openclaw-lancedb-knowledge -g
```

If your installer expects a packaged skill file, download:

```text
https://github.com/JasperYang0609/openclaw-lancedb-knowledge-skill/raw/main/dist/openclaw-lancedb-knowledge.skill
```

## What it includes

- Model-agnostic / Opus-readable skill instructions
- OpenClaw-specific LanceDB workflow instructions
- Portable `knowledge-lancedb` Node template using `@lancedb/lancedb`
- Local-only hash embedding default
- Optional Google Gemini embedding mode after explicit privacy approval, with L2-normalized vectors and a local embedding cache
- Stable `balanced` Gemini profile (768 dimensions) plus an opt-in `high-quality` profile (3072 dimensions, separate cache, full-reindex guard)
- Paragraph-aware chunking and deterministic semantic metadata (`doc_type`, tags, importance) that never depends on an LLM
- Model-agnostic, opt-in AI enrichment via a strict JSONL contract; AI fields are auxiliary, confidence-gated, and cannot overwrite authoritative source metadata
- A source-grounded retrieval benchmark with Hit@K/MRR metrics and a 20-case release gate
- Secret redaction before embedding/indexing, covering common API keys, cloud/chat platform tokens, PEM blocks, URL credentials, and Chinese credential labels
- Incremental indexing and cron wrapper, with embedding-cache compaction and report/log rotation
- Source-map examples for memory, backup summaries, opt-in Discord raw history, project docs, and Obsidian-style vaults
- A read-only exact coverage audit for source chunks, metadata/tags, and embedding identity
- Checksummed restore snapshots containing LanceDB, index state, embedding cache, tag rules, enrichment, and config
- Safe rolling retention for daily snapshots; manual snapshots are never pruned
- Supply-chain-safe bootstrap: fixed `npm ci --ignore-scripts` by default, explicit lifecycle-script opt-in, and a non-executing post-run checker

## Quality and safety defaults

- New installs stay local-only until an external embedding provider is explicitly approved.
- Gemini uses 768 dimensions by default. Choose `--embedding-profile high-quality` only when you accept a one-time 3072-dimensional rebuild.
- AI enrichment is disabled by default and no bundled command uploads private chunks. `enrich:prepare` creates a local redacted JSONL file; a human-approved model workflow produces output; `enrich:validate` rejects malformed rows and attempted core-field overrides.
- Copy `config/benchmark.example.json` to `config/benchmark.json`, replace the 20 examples with corpus-specific ground truth, and run `npm run benchmark -- --release-gate` before claiming a quality improvement.

## Maintainer use of Codex

This project is maintained as part of the OpenClaw ecosystem. We plan to use Codex to review pull requests, improve LanceDB/OpenClaw compatibility, generate tests for indexing and retrieval behavior, and keep installation and source-map documentation current.

API-assisted maintenance should focus on reusable open-source workflows: issue triage, regression checks, documentation updates, and release notes. Codex should not be used to index or inspect private customer memories, transcripts, backups, or project documents.
