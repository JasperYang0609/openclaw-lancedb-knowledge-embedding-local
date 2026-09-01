# OpenClaw LanceDB Knowledge — Qwen Local

**macOS Apple Silicon local-only edition.** Documents and queries are embedded by a managed Qwen3-Embedding-4B sidecar bound to `127.0.0.1`; there is no cloud embedding fallback and no API key is required.

Requirements: Apple Silicon Mac, Python 3, system `curl`, at least 16 GiB RAM and 12 GiB free disk. The first install downloads about 2.70 GiB for the model plus a 10.4 MiB pinned llama.cpp runtime. Initial indexing time depends on corpus size.

Intel Mac, Linux and Windows are not supported by the first installer release. For lower local resource use, use the separate [Gemini cloud edition](https://github.com/JasperYang0609/openclaw-lancedb-knowledge-skill); that edition sends embedding input to Google after its approval gate.

## Install and manage the runtime

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

The installer does not modify PATH, shell startup files, LaunchAgents, OpenClaw production configuration, existing Gemini indexes, schedules, caches, or source documents.

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
