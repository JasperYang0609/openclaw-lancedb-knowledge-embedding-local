# Qwen-Local Fresh-Install Snapshot Automation Design

Date: 2026-09-04
Decision: Approach A approved by Jasper on 2026-09-04

## Problem

`qwen-local integrate-openclaw` transactionally installs the Plugin, Skill, loopback
service, project, and one 06:30 incremental cron. The snapshot engine already supports
checksums, freshness, restore canaries, LanceDB open/row-count checks, and retention,
but the installer does not create or verify the required 06:50 daily snapshot job.
It also treats a committed prior transaction as final instead of reconciling a newer
runtime contract, and its rollback model records only one cron job.

Consequently, a fresh or older customer install can be searchable but lack automatic,
verified recovery snapshots and complete failure alerts.

## Outcome

`./qwen-local integrate-openclaw` becomes both installer and idempotent upgrader for
all Qwen-local-owned OpenClaw assets. A successful ready result proves one exact 06:30
incremental job, one exact 06:50 snapshot job, complete failure alerts, no legacy cron
tools field, a valid local-only runtime, and a tested rollback receipt.

## Owned declarations

- Preserve the existing incremental declaration key for backward compatibility.
- Add one versioned snapshot declaration key owned by this product.
- Both jobs use Asia/Taipei by default unless the installer records another explicit
  IANA timezone. Their argv, cwd, timeouts, no-output timeouts, and output caps are
  fixed by reviewed builders rather than shell strings.
- Every recurring job enables a failure alert after one execution error, excludes
  safe skipped runs, uses a one-hour cooldown, and targets the locally configured
  report destination.
- The initial full-index one-shot also receives an alert while pending. It is tracked
  in the transaction receipt and is not considered a recurring declaration.
- Fresh owned command jobs must omit `payload.toolsAllow`; command-job tool patches are
  unsupported by the pinned OpenClaw CLI. An owned legacy command carrying that field
  fails closed before mutation and requires reviewed recreation. Unknown or look-alike
  jobs are never deleted or disabled automatically.

## Deterministic snapshot job

The packaged template adds a fixed-argv daily snapshot wrapper. At 06:50 it:

1. Resolves the installer-owned project and snapshot roots from the ownership manifest.
2. Rejects symlinks, unsafe ownership/permissions, paths outside the approved local
   roots, and any cloud provider or fallback configuration.
3. Waits a bounded time for the Qwen index lock; timeout is a failure and triggers an
   alert rather than snapshotting a moving database.
4. Reads the trusted successful-index closeout timestamp and expected row count.
5. Creates `daily-YYYY-MM-DD` without overwriting existing snapshots.
6. Verifies the exact manifest and SHA-256 file set, post-closeout freshness, isolated
   restore, LanceDB open, table identity, and row-count equality.
7. Applies retention only after all verification gates pass: keep 30 daily snapshots;
   keep transient `incident-*` and `repair-*` snapshots for seven days with a maximum
   of ten. Manual snapshots are not pruned by the recurring job.

If today's daily snapshot already exists, the wrapper verifies it against the current
closeout. A current valid snapshot returns an idempotent success. A valid but stale
snapshot remains immutable and a bounded `repair-YYYY-MM-DD-*` snapshot is created;
an invalid existing snapshot blocks the run and is not overwritten or deleted.

The snapshot root defaults to a private installer-managed directory under the current
user's home. A custom root requires an explicit absolute path and the same containment,
ownership, regular-directory, and no-symlink checks. Snapshot contents, queries,
vectors, and source text are never sent to a cloud service.

## Upgrade and cron reconciliation transaction

A committed older integration enters upgrade reconciliation instead of returning
early.

1. Preflight the platform, OpenClaw configuration, ownership manifest, loopback
   runtime, current project, installed Skill/Plugin, and complete cron inventory.
2. Save checksummed pre-change copies of installer-owned files and full definitions
   for every owned cron job. The receipt contains no secrets or corpus content.
3. Detect declaration duplicates and non-owned jobs whose resolved argv targets an
   owned wrapper. Preserve unknown jobs and block activation for operator review.
4. Stage and verify the current runtime, Skill, Plugin, snapshot wrapper, and manifest.
5. Create missing owned declarations or update drifted owned declarations in place.
6. Attach alerts without command-job tools flags, reject legacy command tools fields,
   validate exact schemas, and run the existing loopback/readiness checks plus
   snapshot-wrapper dry-run.
7. Commit the new receipt only after full verification.

On failure, remove only declarations created by the current transaction and recreate
or restore every prior owned definition exactly. Restore prior installer-owned files
only after checksum verification. Unknown jobs, Gemini rollback assets, indexes,
caches, corpus files, and snapshots remain untouched.

`verify-openclaw` checks exact enabled count, declaration key, schedule, timezone,
argv, cwd, limits, alert policy, absence of `payload.toolsAllow`, project identity,
snapshot root, local-only provider, Plugin/Skill parity, service identity, and Gateway
readiness. Verification never mutates state.

## Packaging and local synchronization

- The `.skill` and repository package include the daily snapshot wrapper, snapshot
  engine, integration contract, and verification logic.
- Archive parity fails on a missing or drifted wrapper/contract.
- Local synchronization first records hashes for the installed Skill, live project
  config/state, index identity, and cron inventory. Only installer-owned Skill files
  and declarations may change. Project data, LanceDB rows, cache, corpus, Gemini
  rollback assets, and existing snapshots must retain their identities.
- Installed Skill parity ignores only documented installer metadata; executable and
  contract files must byte-match the reviewed archive.

## Verification matrix

- Fresh integration creates exactly one incremental and one snapshot declaration with
  exact 06:30/06:50 ordering, timezone, fixed argv, limits, and alerts.
- Re-running the same version is a no-op; running a newer version upgrades owned files
  and jobs without duplication.
- Missing, stale, duplicate, malformed, legacy-tools, and unknown-collision cron cases
  fail or reconcile according to ownership rules.
- Fault injection at every file and cron mutation phase restores the prior owned state.
- Snapshot tests cover lock wait/timeout, safe same-day reuse, repair creation,
  checksum tamper, missing/extra files, symlinks, traversal, stale closeout, restore
  failure, DB-open failure, row mismatch, and retention ordering.
- Package, installed parity, Python tests, Node tests, dependency audit, secret scan,
  and no-cloud source scan pass.
- An end-to-end temporary-profile install/upgrade/verify/uninstall cycle leaves no
  duplicate managed jobs and restores its pre-install state.

## Security scope and planned OWASP Top 10:2025 evidence

This is a local CLI, Plugin, Skill, loopback service, and cron automation product. It
exposes no web or remote API endpoint, so ASVS v5.0.0 is not applicable; equivalent
controls are manifest ownership, loopback binding, fixed argv, path containment,
checksummed artifacts, transactional rollback, and deterministic negative tests.

- A01 Broken Access Control: installer-owned roots/declarations only; cross-root,
  cross-project, unknown-job, and Gemini-asset negative tests.
- A02 Security Misconfiguration: exact schedule/timezone/argv/alerts/tools checks,
  loopback-only provider validation, and config drift refusal.
- A03 Software Supply Chain Failures: pinned manifests/lockfiles, deterministic Skill
  archive parity, dependency audit, and secret scan.
- A04 Cryptographic Failures: SHA-256 artifact/snapshot/receipt verification and no
  credentials or corpus content in logs or Git.
- A05 Injection: fixed argv with `shell=False`, validated roots/IDs/timezone, and no
  model/retrieval output used as commands.
- A06 Insecure Design: explicit ownership, no cloud fallback, bounded lock wait,
  immutable snapshots, idempotency, rollback, and duplicate-run controls.
- A07 Authentication Failures: not applicable with evidence; no authentication or
  credential store is implemented, and local OpenClaw owns its authenticated control
  channel.
- A08 Software or Data Integrity Failures: manifest identity, snapshot checksum,
  freshness, restore canary, DB-open, row-count, package parity, and rollback receipts.
- A09 Security Logging and Alerting Failures: alerts on incremental, snapshot, and
  pending initial build; bounded redacted phase/run diagnostics; alert canary.
- A10 Mishandling of Exceptional Conditions: timeout, lock contention, corrupt JSON,
  partial mutation, repeated upgrade, stale snapshot, disk failure, rollback failure,
  service restart, and resume tests.

Release is blocked if any A01-A10 item lacks evidence, any P0/P1 remains open, any
managed cron contract is ambiguous, package parity fails, or local-only/provider and
snapshot integrity gates do not pass.

## Non-goals

- No cloud embedding provider, cloud fallback, or upload of corpus/vector data.
- No deletion or automatic disabling of unknown customer jobs.
- No mutation or deletion of retained Gemini rollback assets.
- No overwrite of an existing daily, incident, repair, or manual snapshot.
- No change to search ranking, embedding identity, dimensions, pooling,
  normalization, source-map privacy, or Discord raw opt-in policy.
