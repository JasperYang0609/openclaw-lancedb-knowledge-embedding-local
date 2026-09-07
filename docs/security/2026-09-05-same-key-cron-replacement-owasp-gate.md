# Qwen same-key cron replacement security gate

Date: 2026-09-05
Status: `LOCAL_CANDIDATE_PASS_HUMAN_LIVE_GATE`
Scope: local installer transaction and installer-owned OpenClaw cron replacement.

## Security scope and threat model

- Protected assets: existing cron definitions, unknown/customer jobs, local Qwen
  runtime/index, OpenClaw configuration, transaction receipts, and alert routing.
- Trusted identities: private write-ahead transaction, exact managed declaration
  keys, exact cron IDs and full normalized contract hashes, fixed local code.
- Untrusted inputs: live cron JSON/readback, CLI responses, filesystem state,
  unknown jobs, and interrupted prior transactions.
- Data classification: cron metadata and local paths are internal; corpus/index
  content and credentials remain restricted and must not enter logs or receipts.
- Abuse/failure cases: deletion of an unknown job, same-key collision, inventory
  drift, active-job race, crash between remove/add/checkpoint, partial staging,
  forged readback, rollback failure, secret-bearing diagnostics, and cloud fallback.
- Maximum allowed blast radius: exact installer-owned managed cron IDs whose
  durable receipt graph and current lifecycle contract both validate. Any
  uncertainty, same-ID reuse, or unattributed field fails closed before mutation.

## OWASP Top 10:2025 matrix

- A01 Broken Access Control — `PASS`: original mutation authority is a durable
  exact-ID receipt; newly added authority is a durable nonce intent followed by
  an immediate exact-ID checkpoint. Rollback first validates the full inventory
  partition, definition hashes, intent semantics, ID disjointness, and each live
  lifecycle, then removes under an inventory snapshot guard. Rollback has no
  declaration-key sweep. Forged receipt, same-ID replacement, hostile same-key,
  changed unknown, extra unrelated, nonmanaged-target, and approved-collision
  tests prove unreceipted jobs are preserved.
- A02 Security Misconfiguration — `PASS`: recurring and initial schedules,
  isolated session, fixed argv/cwd/timeouts/env, no-delivery, first-failure alert,
  and descriptions retain exact readback checks. Restorable definitions reject
  invalid timezone/date/resource values, unsupported delivery/session shapes,
  CLI-ambiguous scalar values, NUL, and future unreviewed top-level behavior.
  Every staging/configuration edit explicitly remains disabled; the local-only
  provider scan passed.
- A03 Software Supply Chain Failures — `PASS`: the deterministic 56-file Skill
  matches source; both Node package dry-runs and lockfile-backed production audits
  passed with zero vulnerabilities; Plugin syntax and official validation passed.
- A04 Cryptographic Failures — `PASS`: exact job/full-inventory SHA-256 receipts,
  nonce pre-alert contract hashes, deterministic archive parity, and the candidate
  secret-pattern scan passed. No credentials or corpus contents were added to
  receipts, source, tests, or closeout records.
- A05 Injection — `PASS`: fixed argv, closed and bounded round-trip definitions,
  `shell=False`, safe cron IDs, NUL rejection, CLI option-value canonicalization,
  and direct, shell, and `/usr/bin/env` wrapper collision detection passed.
  Dangerous-exec isolation across 19 production files, Plugin closed-input tests,
  and Python/Node syntax checks also passed.
- A06 Insecure Design — `PASS`: the latest candidate adds a fresh
  contract/disabled/inactive gate before every exact removal, phase-aware
  cross-binding of replacement and Gemini receipts, and a durable resumable
  activation fail-safe that disables uncommitted managed jobs before strict
  rollback. Focused, reconciliation, and full release checks passed locally;
  two sequential independent GPT reviews approved the remediated local
  candidate with no open P0-P3 findings.
- A07 Authentication Failures — `NOT_APPLICABLE_WITH_EVIDENCE`: no public login,
  session, token, or credential flow is added; existing local Gateway control
  plane is unchanged.
- A08 Software or Data Integrity Failures — `PASS`: exact unknown and quiesced
  Gemini hashes, exact managed IDs/contracts/intents, no-extra-ID enforcement, and
  a durable commit-time full-topology receipt are re-read before `committed`.
  Routine verification binds live managed jobs and quiesced Gemini jobs to their
  exact committed IDs/hashes and valid READY/INDEX_BUILDING state while allowing
  unrelated inventory changes. A later reconciliation cannot silently re-baseline
  a missing, enabled, or drifted committed Gemini job.
  Archive/source parity and exact rollback restoration tests passed.
- A09 Security Logging and Alerting Failures — `PASS`: the existing first-failure
  announce contract remains exact, all command output is bounded in saved logs,
  and secret/corpus data is absent. The checkpoint-order test proves alert edits
  occur only after durable ID ownership and while disabled.
- A10 Mishandling of Exceptional Conditions — `PASS`: new focused cases cover
  late-active and malformed runtime state before removal, failures after the
  first and second activation edits, failures after all jobs enable, unrelated
  unknown-job drift, final committed verification failure, interrupted disarm,
  interrupted compensation restart, and best-effort handling when one target is
  missing, drifted, or cannot be edited. The full suite and two sequential
  independent reviews passed.
  Live preflight also exposed a macOS reboot boundary where the APFS device
  number changed while the private rollback snapshot's inode and durable random
  marker did not. Cleanup now accepts only a device-number-only rebind after the
  exact marker validates; inode, marker, permission, path, and symlink negatives
  remain fail-closed. The post-remediation suite passed `536` tests.

## ASVS v5.0.0 boundary

`NOT_APPLICABLE_WITH_EVIDENCE`: this repair adds no Web/API endpoint, browser
session, authentication, tenant, or HTTP application surface. Equivalent controls
are the local transaction/rollback contract, filesystem ownership, exact cron
inventory authorization, fixed argv, bounded outputs, and negative recovery tests.

## AI security overlay

No model output drives this installer transaction. Corpus and search results are
untrusted data and cannot become commands. Qwen remains bound to `127.0.0.1` with
no cloud fallback. Live deployment remains a human-authorized operation.

## Closeout requirements

Every `IN_PROGRESS` row must become `PASS`, `BLOCKED`, or
`NOT_APPLICABLE_WITH_EVIDENCE` with named test/log evidence before release. Any
P0/P1 finding, unknown-job mutation, rollback uncertainty, or non-local provider
keeps live status `PARTIAL`.

## Named verification evidence

The logs, suite totals, and artifact hash below are from the current uncommitted
candidate after the latest safety edits. They satisfy the local validation gate
but do not replace independent review or authorize live mutation.

- Access-control and transaction negatives:
  `test_managed_replacement_blocks_inventory_drift_before_remove`,
  `test_success_inventory_drift_fails_without_deleting_unreceipted_job`,
  `test_hostile_concurrent_same_key_job_is_never_deleted_by_rollback`, and
  `test_uncheckpointed_staging_intent_requires_zero_or_one_exact_match`.
- Exceptional-condition recovery:
  `test_crash_after_first_managed_remove_recovers_from_write_ahead_receipt`,
  `test_crash_after_first_replacement_add_recovers_without_created_id_checkpoint`,
  `test_initial_add_crash_before_id_checkpoint_adopts_unique_staged_job`, and
  `test_rollback_restore_add_crash_resumes_exact_staging_intent`.
- Ordering and disabled-state invariant:
  `test_cron_edits_observe_durable_id_checkpoint_and_never_transiently_enable`.
- Full topology receipt and routine verification policy:
  `test_integrate_replaces_same_key_jobs_from_write_ahead_receipt_before_strict_add`.
- Latest Gemini receipt selection: `10 passed` in
  `20260907_224948_qwen-gemini-receipt-p2-focused.log`; receipt/re-entry
  security selection: `36 passed` in
  `20260907_162302_qwen-p2-reentry-graph-focused.log`.
- Reconciliation suite: `272 passed` in
  `20260907_225111_qwen-gemini-receipt-p2-reconciliation-rerun.log`.
- Full Python suite: `534 passed` in
  `20260907_225140_qwen-gemini-receipt-p2-full-python.log`.
- Post-reboot device-rebind remediation: `536 passed` in
  `20260907_232800_qwen-reboot-device-rebind-full-python.log`; the accepted
  device-only case and rejected inode-drift case run alongside the existing
  same-path replacement and marker-tamper negatives.
- Skill archive test/parity: `PASS`, 56 source files, with rebuild/parity evidence
  in `20260907_225217_qwen-gemini-receipt-p2-check-skill.log` and
  `20260907_225224_qwen-gemini-receipt-p2-archive-test.log`.
- Template Node: `28 passed`; Plugin Node: `5 passed`; official Plugin validation:
  `PASS`; both production dependency audits: `0 vulnerabilities`. Evidence is in
  `20260907_223057_qwen-repair-template-gates-latest.log`,
  `20260907_223142_qwen-repair-plugin-tests-latest.log`, and
  `20260907_223209_qwen-repair-plugin-validate-latest.log`.
- Dangerous-exec isolation, Python compile, package dry-runs, post-run contract,
  candidate secret-pattern scan, local-only boundary scan, and `git diff --check`:
  `PASS` in `20260907_223120_qwen-repair-template-postrun-latest.log`,
  `20260907_223209_qwen-repair-plugin-validate-latest.log`,
  `20260907_225224_qwen-gemini-receipt-p2-archive-test.log`,
  `20260907_225231_qwen-gemini-receipt-p2-pycompile.log`,
  `20260907_225237_qwen-gemini-receipt-p2-diff-check.log`,
  `20260907_225327_qwen-gemini-receipt-p2-local-only.log`, and
  `20260907_225333_qwen-gemini-receipt-p2-secret.log`.
- Built artifact SHA-256:
  `f7ef67cc8c8b6f3a7d175097e7d3c0b7bf089211bc6c52e6cad48afc79c6d5ca`.

## Security closeout

- `OWASP_A01_A10_STATUS`: A07 is `NOT_APPLICABLE_WITH_EVIDENCE`; A01-A06 and
  A08-A10 are `PASS` on current local evidence and independent review.
- `ASVS_OR_EQUIVALENT_SCOPE`: ASVS v5.0.0 is not applicable because this change
  adds no Web/API endpoint, browser session, authentication, tenant, or HTTP
  application surface. Equivalent local transaction, fixed-argv, ownership,
  integrity, rollback, and exceptional-condition controls are enumerated above.
- `ASVS_5_0_0_REGISTER_COUNTS`: 0 Web/API requirements in scope; 0 fail; boundary
  evidence is the unchanged local CLI/cron/filesystem architecture and local-only
  provider verification.
- `BUSINESS_LOGIC_NEGATIVE_TESTS`: unauthorized same-key creation, unknown-job
  drift/addition, ambiguous candidate, add-response loss, partial removal,
  re-entry, restore re-entry, and no-extra-topology cases passed.
- `AI_SECURITY_OVERLAY`: no model output or retrieved corpus data can authorize or
  construct this transaction; no AI tool or cloud provider boundary changed.
- `SAST_DAST_DEPENDENCY_SECRET_SCAN`: local static/dangerous-exec, syntax,
  dependency, package, secret-pattern, and provider-boundary gates passed. DAST is
  not applicable because no network endpoint changed.
- `THREAT_MODEL_REVIEW`: earlier independent review found three P2 and one P3;
  the first current-candidate review then found one blocking P2 in the preserved
  Gemini receipt derivation and one documentation P3. The P2 now derives the
  disabled Gemini hashes from the immutable original definitions and rejects
  missing, extra, malformed, and re-baselined receipts. Remediation re-review
  passed, and a second attack-oriented review returned a conditional local
  commit/push pass. Both final reviews reported P0/P1/P2/P3 = 0/0/0/0.
- `OPEN_P0_P1_P2_P3`: `0/0/0/0` for the local candidate.
- `ACCEPTED_RESIDUAL_RISK_OWNER`: pending Human Gate for the OpenClaw CLI's
  non-CAS final read-to-`rm` race. Mitigations are the integration lock, complete
  immediate inventory re-read, exact target hash, disabled/inactive checks, and
  a controlled window with no external managed-cron mutation.
- `COMMIT`: exact-reviewed implementation candidate `9f14f61`; this security
  closeout record is committed separately after recording that immutable hash.
- `RELEASE_DECISION`: `LOCAL_COMMIT_PASS`; live integration remains
  `HUMAN_GATE`. No live cron mutation, deployment, or customer-data mutation is
  authorized by this closeout.

## Post-cutover compatibility security addendum (2026-09-08)

- A01 / A04 / A08 — `PASS`: the larger input allowance is not a new generic
  default.  The 4 MiB default remains in force and only the known
  `index-state.json` call sites opt into a hard 32 MiB maximum.  Stable file
  identity, owner, link-count, symlink, permissions and JSON-object validation
  remain unchanged.  Tests prove default rejection, explicit acceptance and
  rejection above the absolute ceiling.
- A05 / A08 / A10 — `PASS`: writable legacy snapshots are classified as
  untrusted and preserved without reuse or mutation.  The runner creates a new
  immutable repair snapshot and verifies checksum, restore canary, database open
  and exact row count.  Symlinks, hard links, special files, immutable checksum
  drift and unsafe paths still stop the run before pruning or receipt success.
- A06 — `PASS`: both shipped production dependency trees
  audit at zero vulnerabilities.  The pinned OpenClaw development-only tree has
  newly published advisories; it is excluded from runtime packages and is not
  invoked by the Qwen cron jobs.  Upgrading the host OpenClaw release is a
  separate compatibility-controlled change, not silently bundled into this
  repair.
- A09 — `PASS`: the failed snapshot wrote the existing bounded redacted error
  receipt and created no recovery artifact.  Successful remediation must replace
  that state only after full verification.
- Regression evidence: focused snapshot/security `57 passed`, full Python
  `538 passed`, Template Node `28 passed`, Plugin Node `5 passed`, production
  audits `0 vulnerabilities`, and all package/static/local-only/secret gates
  `PASS`.
- `OPEN_P0_P1_P2_P3`: `0/0/0/0` for this compatibility remediation.  The
  development-only OpenClaw advisory set is tracked as a scoped dependency
  residual and does not change the runtime repair decision.
