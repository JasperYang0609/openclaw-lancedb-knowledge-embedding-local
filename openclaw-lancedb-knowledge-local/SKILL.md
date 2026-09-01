---
name: openclaw-lancedb-knowledge-local
description: Build and operate a macOS Apple Silicon OpenClaw knowledge index using local Qwen3 embeddings and LanceDB. Use for local-only indexing, source-cited search, backup summaries, project documents, runtime lifecycle, and safe snapshots when corpus text must not be sent to a cloud embedding provider.
---

# OpenClaw LanceDB Knowledge Local

This skill is local-only. Embedding input may go only to the managed Qwen sidecar at `127.0.0.1:18888`. Do not add a cloud provider or reuse an index built by another embedding identity.

## Setup

- Confirm the host is macOS Apple Silicon with at least 16 GiB RAM and 12 GiB free disk.
- From the repository root, run `./qwen-local install`, then `./qwen-local health`.
- Bootstrap an isolated project with `scripts/bootstrap_openclaw_lancedb.py`.
- Review `config/source-map.json`; raw Discord messages remain explicit opt-in and are marked `LOCAL_ONLY`.
- Run `npm ci --ignore-scripts`, `npm test`, `npm run scan`, and `npm run index`.

## Runtime operations

Use only the repository-level `qwen-local` command for lifecycle operations:

```bash
./qwen-local install
./qwen-local verify
./qwen-local status
./qwen-local health
./qwen-local stop
./qwen-local start
./qwen-local uninstall
```

The installer verifies pinned artifact sizes and SHA-256 values. It refuses unsafe targets, archive traversal, symlinks, unknown uninstall files, mismatched manifests, stale PID identity, non-loopback endpoints and unsupported platforms.

## Index operations

Within the bootstrapped project:

```bash
npm run scan
npm run index
npm run audit
npm run search -- "project status" -- --limit 5
npm run incremental
npm run benchmark -- --file config/benchmark.json --release-gate
```

Search answers must cite source paths. Changing model, dimensions, pooling, normalization, runtime revision or digest requires a new index identity and full rebuild.

Do not modify production configuration, Gemini assets, shell startup files, PATH or background services. For cloud embedding, use the separate repository at <https://github.com/JasperYang0609/openclaw-lancedb-knowledge-skill>.
