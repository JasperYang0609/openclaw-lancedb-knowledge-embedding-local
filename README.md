# OpenClaw LanceDB Knowledge — Qwen Local

**macOS Apple Silicon local-only edition.** Documents and queries are embedded by a managed Qwen3-Embedding-4B sidecar bound to `127.0.0.1`; there is no cloud embedding fallback and no API key is required.

Requirements: Apple Silicon Mac, Python 3, system `curl`, at least 16 GiB RAM and 12 GiB free disk. The first install downloads about 2.70 GiB for the model plus a 10.4 MiB pinned llama.cpp runtime. Initial indexing time depends on corpus size.

Intel Mac, Linux and Windows are not supported by the first installer release. For lower local resource use, use the separate [Gemini cloud edition](https://github.com/JasperYang0609/openclaw-lancedb-knowledge-skill); that edition sends embedding input to Google after its approval gate.

## One-command OpenClaw integration

```bash
./qwen-local integrate-openclaw
```

This command installs and verifies the pinned Qwen runtime, creates or adopts the isolated Qwen LanceDB project, installs the `openclaw-lancedb-knowledge-local` Plugin and Skill, registers the read-only `local_knowledge_search` tool, installs the per-user launchd service, creates one idempotent daily incremental command job, validates configuration, and safely restarts the OpenClaw Gateway.

After the index is reconciled, OpenClaw proactively uses `local_knowledge_search` for questions about prior decisions, project status, handoffs, meeting notes, backups and internal documents. The user does not need to ask it to search. The Skill also tells OpenClaw not to search for unrelated general-knowledge or creative requests. Search and indexing have no Gemini or other cloud embedding fallback.

If a fresh full index is still running, the tool returns `INDEX_BUILDING` and never silently queries Gemini. Verify or revert the integration with:

```bash
./qwen-local verify-openclaw
./qwen-local rollback-openclaw
./qwen-local uninstall-openclaw
```

The transaction backs up the existing OpenClaw configuration and local Skill, only disables exactly identified Gemini incremental jobs, and preserves all Gemini indexes, caches and settings for emergency rollback. Unknown ownership or configuration drift fails closed.

## Manage only the Qwen runtime

```bash
./qwen-local install
./qwen-local status
./qwen-local health
./qwen-local stop
./qwen-local start
./qwen-local verify
./qwen-local uninstall
```

`install` pins Qwen model revision `f4602530...` and official llama.cpp `b10625`, resumes partial downloads, verifies size and SHA-256, safely extracts into staging, starts the loopback-only sidecar, and runs an embedding canary. `uninstall` removes only manifest-owned files and fails closed when unknown files or links are present.

The runtime-only commands do not modify PATH, shell startup files, LaunchAgents, OpenClaw configuration, existing Gemini indexes, schedules, caches, or source documents. Only the explicit `integrate-openclaw` command performs the documented transactional OpenClaw integration.

## Install the skill archive

The distribution artifact is `dist/openclaw-lancedb-knowledge-local.skill`. After installing it, bootstrap an isolated index project:

```bash
python3 openclaw-lancedb-knowledge-local/scripts/bootstrap_openclaw_lancedb.py \
  --target ~/.openclaw/workspace/knowledge-lancedb-qwen-local \
  --workspace ~/.openclaw/workspace \
  --npm-install
```

Then run `npm test`, `npm run scan`, `npm run index`, and `npm run search -- "your query"`. The default table, data directory, cache identity and state are Qwen-specific and do not reuse Gemini vectors.

Historical Qwen/Gemini comparison reports under `docs/reports/` are selection evidence only; they are not runtime dependencies.
