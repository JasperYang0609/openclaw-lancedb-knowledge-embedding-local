# OpenClaw LanceDB Knowledge — Qwen Local

**macOS Apple Silicon local-only edition.** Documents and queries are embedded by a managed Qwen3-Embedding-4B sidecar bound to `127.0.0.1`; there is no cloud embedding fallback and no API key is required.

Requirements: Apple Silicon Mac, Python 3, system `curl`, at least 16 GiB RAM and 12 GiB free disk. The first install downloads about 2.70 GiB for the model plus a 10.4 MiB pinned llama.cpp runtime. Initial indexing time depends on corpus size.

Intel Mac, Linux and Windows are not supported by the first installer release. For lower local resource use, use the separate [Gemini cloud edition](https://github.com/JasperYang0609/openclaw-lancedb-knowledge-skill); that edition sends embedding input to Google after its approval gate.

## One-command OpenClaw integration

```bash
./qwen-local integrate-openclaw \
  --report-channel discord \
  --report-to channel:123456789012345678 \
  --report-account-id default
```

Replace the example channel ID with the customer's dedicated backup-monitoring Discord channel. A fresh integration fails closed unless this explicit alert destination is supplied.

This command installs and verifies the pinned Qwen runtime, creates or adopts the isolated Qwen LanceDB project, installs the `openclaw-lancedb-knowledge-local` Plugin and Skill, registers the read-only `local_knowledge_search` tool, installs the per-user launchd service, and reconciles two installer-owned jobs: the 06:30 incremental index and the 06:50 verified recovery snapshot. Both jobs use fixed argv, bounded runtime/output, no delivery spam, and a failure alert after the first real error. The installer validates configuration and safely restarts the OpenClaw Gateway only inside its rollback-capable transaction.

After the index is reconciled, OpenClaw proactively uses `local_knowledge_search` for questions about prior decisions, project status, handoffs, meeting notes, backups and internal documents. The user does not need to ask it to search. The Skill also tells OpenClaw not to search for unrelated general-knowledge or creative requests. Search and indexing have no Gemini or other cloud embedding fallback.

If a fresh full index is still running, the tool returns `INDEX_BUILDING` and never silently queries Gemini. Verify or revert the integration with:

```bash
./qwen-local verify-openclaw
./qwen-local rollback-openclaw
./qwen-local uninstall-openclaw
```

The snapshot job waits for indexing to finish, then atomically owns the same index lock for the entire copy and verification transaction so a new index run cannot start between assets. Index wrappers treat only an existing owner-safe directory as normal contention; files, symlinks, unsafe permissions, and lock-creation failures fail nonzero and write an error health receipt. The snapshot requires SHA-256 integrity, post-index freshness, an isolated restore canary, LanceDB open, exact table identity, and matching row count. Daily snapshots are immutable and never overwritten. A stale same-day snapshot creates a separate repair snapshot; a tampered same-day snapshot blocks and alerts. Retention keeps 30 daily snapshots and a seven-day/ten-item combined window for incident/repair snapshots, while manual snapshots remain untouched.

The transaction backs up the existing OpenClaw configuration, installed Plugin and Skill, runtime contract files, health receipt, and full definitions of installer-owned jobs. Existing same-ID Plugins are replaced explicitly and can be restored byte-for-byte if a later gate fails; the supported `node_modules/openclaw` package link is preserved as a link without copying its target, while traversal or unexpected links fail closed. Plugin, Skill, configuration, plist, and launchd rollback actions are checkpointed separately, so a failure before a component changes cannot remove its prior installation. Launchd activation and rollback use bounded retry plus service readback to tolerate the short macOS teardown window without hiding a persistent failure. Each installer-created runtime lock identity is write-ahead checkpointed and directory-synced before the next blocking wait; interrupted transaction writes use unique staging names. Recovery is bound to the private transaction's canonical custom snapshot root, rejects conflicting CLI overrides, and removes only exact installer-created stale locks. The installer only disables exactly identified Gemini incremental jobs and preserves all Gemini indexes, caches and settings for emergency rollback. Unknown/look-alike jobs are not changed; ambiguous ownership or configuration drift fails closed. A committed older installation is reconciled in place, while reinstalling an exact current contract is a no-op. If automatic rollback is incomplete, the CLI preserves that recovery state and does not restart the prior manual runtime into a possibly active managed service; a failed restart after verified rollback is reported together with the primary integration failure.

If preflight finds the one supported pre-declaration Qwen incremental job already disabled, it still blocks by default and prints only its safe job ID, role, and **ID-inclusive** normalized-contract SHA-256. After independently reviewing that exact disabled job, an operator may rerun with all three approval options together:

```bash
./qwen-local integrate-openclaw \
  --report-channel discord \
  --report-to channel:YOUR_MONITORING_CHANNEL \
  --approve-disabled-collision-job-id ID_FROM_CURRENT_DIAGNOSTIC \
  --approve-disabled-collision-job-sha256 ID_INCLUSIVE_SHA256_FROM_CURRENT_DIAGNOSTIC \
  --approve-disabled-collision-role incremental
```

Do not reuse an older hash calculated without the job ID. The approved job remains customer-owned unknown inventory: the installer never edits, enables, disables, removes, or adopts it, and any ID, hash, enabled-state, declaration-key, role, or contract drift blocks before mutation. Only the safe ID/hash/role receipt is stored, and automatic reuse is allowed only from the private committed transaction receipt. Snapshot collisions and all other unknown jobs remain unapprovable and fail closed.

By default snapshots remain in the private integration state directory. Operators can configure an absolute private root and the failure-alert destination explicitly:

```bash
./qwen-local integrate-openclaw \
  --snapshot-root "$HOME/OpenClawBackups/qwen-local" \
  --timezone Asia/Taipei \
  --report-channel discord \
  --report-to channel:YOUR_MONITORING_CHANNEL
```

The Qwen component also writes a private, bounded `backup-health-component.v1` receipt for a consolidated plain-language backup report. It never puts source text, queries, vectors, corpus content, tokens, or API keys in that receipt.

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
