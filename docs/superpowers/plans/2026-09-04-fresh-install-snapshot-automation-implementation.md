# Qwen-Local Snapshot Automation Implementation Plan

Date: 2026-09-04
Approved design: `../specs/2026-09-04-fresh-install-snapshot-automation-design.md`
Approval: Jasper, 2026-09-04 (`規格確認，開始實作`)

## Objective

Make `./qwen-local integrate-openclaw` an idempotent installer/upgrader for the full
Qwen-local runtime contract: one 06:30 incremental job, one 06:50 verified snapshot
job, complete alerts, local-only enforcement, exact verification, and transactional
rollback. Export bounded local health evidence for the backup product's consolidated
plain-language report.

## In scope

- Preserve the incremental declaration key; add a versioned snapshot declaration.
- Reconcile missing/drifted owned jobs instead of treating an old committed receipt
  as final.
- Add exact failure alerts to incremental, snapshot, and pending initial-build jobs.
- Package a fixed-argv snapshot wrapper with bounded lock wait, immutable same-day
  handling, repair snapshot fallback, checksum/freshness/restore/LanceDB/row-count
  verification, and the approved retention policy.
- Add full owned-file/job receipts, rollback, collision blocking, exact
  `verify-openclaw`, package parity, and temporary-profile lifecycle tests.
- Emit a bounded redacted local run-result receipt suitable for the consolidated
  health renderer; never post per-file/vector engineering details directly.

## Out of scope / forbidden

- No cloud embedding/fallback or corpus/vector upload.
- No mutation of ranking, embedding identity, source-map privacy, or raw opt-in.
- No deletion/disable of unknown jobs or Gemini rollback assets.
- No overwrite of immutable snapshots and no restore into the live index.

## Delivery batches

1. Characterize current integration transaction, cron schema, snapshot engine, and
   package parity with regression tests.
2. Implement snapshot wrapper and health receipt with adversarial path/lock/tamper
   tests.
3. Implement owned declaration builders, plan/apply/verify reconciliation, alerts,
   checksummed receipts, and rollback fault injection.
4. Wire install, upgrade, verify, rollback, uninstall, and package/archive parity.
5. Run Python/Node suites, dependency/secret/no-cloud scans, and a temporary-profile
   fresh-install/reinstall/upgrade/collision/failure/rollback lifecycle.
6. Synchronize reviewed files to the local installed Skill/runtime only after repo
   gates pass; preserve index rows, corpus, caches, snapshots, Gemini assets, and
   unknown cron jobs.

## Acceptance evidence

- Exactly one enabled 06:30 incremental and one enabled 06:50 snapshot declaration,
  with exact timezone, fixed argv/cwd/limits, alerts, and no legacy tools field.
- Reinstall is a no-op; an older receipt upgrades without duplicates; injected
  failures restore every prior owned definition/file.
- Snapshot covers bounded lock wait, immutable reuse/repair, exact SHA-256 file set,
  closeout freshness, isolated restore, DB open/table identity/row count, and retention
  of 30 daily plus seven-day/max-ten transient snapshots.
- Runtime remains loopback-only and no-cloud; package, installed files, and contracts
  are byte-identical where required.
- The local health receipt is bounded, redacted, and consumable without corpus text.

## Security scope and closeout requirements

Assets are local index data, model/runtime identity, cron declarations, installed
Skill/Plugin files, snapshots, and rollback receipts. Trust boundaries are CLI input,
OpenClaw cron JSON, local filesystem paths, loopback service responses, and package
artifacts. Unknown jobs and retrieved/source text are untrusted.

OWASP A01-A10 evidence must cover owned-root/job access, exact configuration and
loopback-only enforcement, supply-chain/dependency/secret checks, SHA-256 integrity,
fixed argv/path validation, idempotency and rollback, authentication N/A evidence,
snapshot/package integrity, end-to-end alerts, and exceptional-condition fault
injection. ASVS v5.0.0 is not applicable because there is no Web/API surface. Release
is blocked on missing evidence, open P0/P1, any cloud fallback, package drift,
rollback failure, or mutation of customer index/corpus/snapshot/Gemini assets.

## Handoff format

Implementer reports changed files, tests/logs, risks, commit candidate, and
`COMPLETE|BLOCKED|NEEDS_PM`. A separate reviewer examines the diff and evidence,
runs independent negative tests, and alone decides release readiness.
