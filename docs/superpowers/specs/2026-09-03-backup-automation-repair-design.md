# Backup Automation Repair Design

## Context

The production OpenClaw workspace now uses the Qwen-local LanceDB project, but two
incremental cron jobs are enabled and the daily snapshot still targets the retained
Gemini rollback project. The shipped snapshot helper also assumes `data/lancedb`,
while a Qwen-local project stores its database under `data/qwen-local-lancedb`.

## Goal

- Keep exactly one enabled incremental job for the managed Qwen project.
- Snapshot the active Qwen database without overwriting legacy Gemini snapshots.
- Keep restore verification bound to the database path recorded by the manifest.
- Preserve local-only privacy and all existing rollback assets.

## Considered approaches

1. **Provider-aware manifest (selected).** Resolve one allowlisted database directory
   from the project, record it in the snapshot manifest, and use the recorded path for
   restore/database verification. This supports both Gemini and Qwen projects while
   retaining strict path validation.
2. Rename or symlink the Qwen database to `data/lancedb`. Rejected because it changes
   the active runtime layout and introduces an unsafe symlink boundary.
3. Copy the Qwen project with a generic filesystem backup only. Rejected as the daily
   primary because it would lose the existing snapshot-specific checksum, restore,
   database-open, row-count, freshness, and retention gates.

## Design

The snapshot helper will accept only the two known relative database directories:
`data/lancedb` and `data/qwen-local-lancedb`. Creation must find exactly one existing
real directory, copy it with the common required assets, and record `databasePath` in
`snapshot-manifest.json`. Existing manifests without the field remain verifiable and
default to `data/lancedb`; new database-open checks use the validated recorded path.

Cron inventory parsing will support the current `payload.argv` schema and the legacy
`payload.command.argv` schema. Verification will reject more than one enabled job
whose resolved argv targets the managed Qwen incremental wrapper, even if only one has
the declaration key. New integrations default to 06:30 Asia/Taipei so indexing runs
after Discord daily sync and outside the overnight backlog window. The local repair
keeps the canonical declaration job at that time and disables the unmanaged duplicate.

The daily Qwen snapshot uses a new Qwen-specific backup root. Existing Gemini daily
snapshots remain immutable rollback evidence. Monthly workspace backup includes both
the retained Gemini project and the active Qwen project.

## Error handling

- Zero or multiple supported database directories fails closed.
- Symlinked database directories or manifest paths outside the supported allowlist
  fail closed.
- A duplicate enabled managed incremental job makes integration verification fail.
- A failed snapshot, checksum, restore canary, database-open, or row-count check does
  not replace or delete an existing snapshot.

## Validation

- Unit tests cover Gemini layout, Qwen layout, ambiguity, symlinks, current/legacy cron
  JSON schemas, and duplicate enabled Qwen jobs.
- Full Python and Node suites, package parity, secret scan, and dependency audit run.
- Production repair creates a new Qwen snapshot, verifies checksums, performs an
  isolated restore canary, opens LanceDB, and checks the expected row count.
- Final cron inventory proves one enabled Qwen incremental job and correct paths.

## Security scope (OWASP Top 10:2025)

- A01: database and snapshot paths are allowlisted and containment-checked.
- A02: snapshot SHA-256 and exact manifest verification remain mandatory.
- A03: commands use fixed argv; no untrusted shell interpolation is introduced.
- A04: immutable legacy snapshots and fail-closed ambiguity handling prevent unsafe
  recovery behavior.
- A05: duplicate cron and wrong-project configuration become explicit failures.
- A06: dependency audit and pinned runtime checks remain required.
- A07: no authentication or credential flow changes.
- A08: manifest asset identity, database-open, and row-count checks are preserved.
- A09: cron failures keep failure alerts and produce bounded, redacted diagnostics.
- A10: local-only embedding and loopback-only provider policy remain unchanged.

Web/API ASVS register: not applicable; this change is a local CLI/automation repair
and exposes no web or API endpoint.
