# Same-key managed cron replacement repair

Date: 2026-09-05
Status: `LOCAL_CANDIDATE_PASS_HUMAN_LIVE_GATE`
Branch: `fix/qwen-same-key-cron-replace-20260905`

## Context

OpenClaw 2026.7.1-2 rejects `cron add` when a job with the same
`declarationKey` already exists. The Qwen installer currently quiesces the prior
owned job but leaves it present, so an upgrade fails before disabled staging and
then rolls back.

## Goal

Make an existing installer-owned Qwen cron upgrade transactional on the current
CLI: quiesce, durably record the exact replacement set, remove only those exact
owned IDs, stage replacements disabled, verify exact readback, and activate only
after the complete recurring topology passes.

## In scope

- Existing jobs under the three Qwen managed declaration keys: incremental,
  snapshot, and one-shot initial index.
- Write-ahead replacement identity in the integration transaction.
- Exact pre-removal and post-removal inventory verification.
- Existing rollback/recovery restoration from the pre-change cron definitions.
- Regression tests for live duplicate-key semantics, unknown-job preservation,
  drift, partial failure, and crash recovery.

## Out of scope / forbidden

- No schedule, payload, timeout, alert, delivery, source-map, embedding model, or
  local-only endpoint changes.
- No Gemini/cloud fallback and no embedding identity change.
- No deletion, adoption, or mutation of unknown jobs or the separately approved
  disabled collision.
- No weakening of exact readback, rollback, or inventory gates.
- No Daily Backup deployment until this Qwen transaction is committed.

## Transaction design

1. Preflight and checksum the complete cron inventory.
2. Persist rollback definitions and exact target IDs before any cron mutation.
3. Disable owned migration targets and wait for executions to quiesce.
4. Persist the exact same-key replacement IDs before removal.
5. Re-read and verify the complete quiesced inventory, then remove only those
   exact IDs whose declarations are installer-managed.
6. Re-read and require all non-replaced jobs to remain contract-identical and all
   replacement declarations/IDs to be absent.
7. Before each recurring or initial add, persist a transaction-unique staging
   description, declaration, role, canonical description, and exact disabled
   pre-alert contract hash. Zero exact candidates permits one add; one exact
   candidate is adopted after an interrupted add response; ambiguity, a
   declaration collision, or contract drift fails closed.
8. Immediately checkpoint the returned/adopted job ID into the intent and exact
   managed-ID set before any alert or description edit. All intermediate edits
   explicitly retain the disabled state.
9. Apply the same nonce-intent, exact-candidate adoption, immediate ID checkpoint,
   and disabled-edit sequence to rollback restoration, using a unique staging
   name so a definition with no description can still round-trip. Restore only
   definitions captured in the durable pre-change receipt.
10. Verify the complete success inventory before configuration restart and again
    at activation: exact unchanged unknown-job hashes, exact quiesced Gemini
    hashes, exact managed IDs/contracts/intents, and no missing or extra IDs.
11. Persist a commit-time full-topology hash receipt in
    `commit_closeout_pending`, re-read and compare the full inventory, and only
    then write `committed`. Routine post-commit verification validates the
    durable receipt metadata, owned contracts, and exact quiesced Gemini IDs and
    hashes without permanently freezing legitimate later changes to unrelated
    jobs. A later repair may not re-baseline Gemini receipt drift.
12. On any failure or restart, first validate the complete receipt graph:
    preflight inventory partition, original definitions and hashes, staging and
    restore intents, disjoint IDs, and every live lifecycle state. Only then may
    exact original or created IDs be removed under a re-read snapshot guard.
    Same-ID replacement, future unreviewed fields, malformed CLI values, or any
    ambiguity fails before runtime or cron mutation. Never sweep by declaration
    key. Restore all captured definitions disabled, verify the full topology,
    then activate the intended set as one compensated phase.
13. Every exact cron removal re-reads the inventory immediately before `rm` and
    requires the target contract to remain exact, disabled, and not active.
    Any present malformed runtime state fails closed for managed replacement,
    legacy migration, and rollback removal through the shared guard. An absent
    runtime marker follows the OpenClaw list contract for an inactive job.
14. Before activation, durably arm an exact-ID fail-safe bound to configured
    staging intents, and keep it armed through the final verification of the
    committed receipt. Disarm only after that verification succeeds. An armed
    committed receipt is restart-resumable; verification failure first invokes
    compensation. Compensation attempts every exact target even when another is
    missing, drifted, or fails its edit, then reports incomplete recovery after
    preserving all independently recoverable jobs as disabled. Unrelated
    unknown-job drift is tolerated by compensation but remains rejected by the
    subsequent exact rollback.
15. OpenClaw currently exposes ID-only `cron rm`, not a conditional compare-and-
    delete operation. The installer therefore minimizes, but cannot eliminate,
    the final read-to-remove race: it holds its integration lock, re-reads the
    complete inventory immediately before each removal, and requires the exact
    target to remain contract-identical, disabled, and inactive. Any live
    cutover remains a separate Human Gate, and an external actor must not mutate
    the managed cron inventory during that controlled transaction.

## Acceptance

- A strict fake matching live duplicate-key behavior rejects add-before-remove.
- Existing managed jobs are removed by exact ID before staging; unknown,
  approved-collision, legacy, and unrelated jobs are untouched.
- Inventory drift blocks before replacement removal.
- Removal/readback/staging failures reach exact rollback or a typed incomplete
  rollback state; they never commit.
- A forced process interruption after removal is recoverable from durable state.
- Targeted tests, full Python suite, Node/package checks, secret scan, and
  `git diff --check` pass.
- Live transaction receipt is `committed`; installed commit, two owned cron
  readbacks, launchd, health receipt, and local search agree.

## Current candidate verification evidence

The labels and counts below describe the current uncommitted source candidate
after the removal guard, activation compensation, and receipt-graph changes.
All local release gates have been rerun. Two sequential GPT independent reviews
completed: the first found one blocking P2 in preserved Gemini receipt
derivation and one documentation P3; remediation re-review closed both with
P0/P1/P2/P3 = 0/0/0/0. The second attack-oriented review returned a conditional
commit/push pass with P0/P1/P2/P3 = 0/0/0/0. These results do not authorize a
live transaction.

- Same-key remove-before-add and durable replace receipt: `PASS` —
  `test_same_key_upgrade_removes_exact_owned_ids_before_strict_add`,
  `test_integrate_replaces_same_key_jobs_from_write_ahead_receipt_before_strict_add`.
- Unknown, legacy, Gemini, and approved-collision preservation: `PASS` —
  `test_managed_replacement_preserves_nonmanaged_targets_and_unknown_jobs`, the
  full reconciliation suite, and the final full Python suite.
- Pre-removal inventory drift: `PASS` —
  `test_managed_replacement_blocks_inventory_drift_before_remove`.
- Remove/add crash recovery with durable exact authority: `PASS` —
  `test_crash_after_first_managed_remove_recovers_from_write_ahead_receipt` and
  `test_crash_after_first_replacement_add_recovers_without_created_id_checkpoint`.
- Hostile/concurrent and ambiguous staging behavior: `PASS` —
  `test_success_inventory_drift_fails_without_deleting_unreceipted_job`,
  `test_hostile_concurrent_same_key_job_is_never_deleted_by_rollback`, and
  `test_uncheckpointed_staging_intent_requires_zero_or_one_exact_match`.
- Initial and rollback add-response crash recovery: `PASS` —
  `test_initial_add_crash_before_id_checkpoint_adopts_unique_staged_job` and
  `test_rollback_restore_add_crash_resumes_exact_staging_intent`.
- ID-before-edit and never-transiently-enabled ordering: `PASS` —
  `test_cron_edits_observe_durable_id_checkpoint_and_never_transiently_enable`.
- Full success/activation/commit topology and non-freezing routine verification:
  `PASS` — the end-to-end strict integration test asserts the commit receipt,
  exact full inventory, and successful routine verification after a legitimate
  unrelated-job replacement. Routine verification also binds recurring and
  initial jobs to their exact committed IDs, rejects an initial job for a READY
  index, and rejects unknown index states.
- Receipt-graph, same-ID reuse, CLI round-trip, and durable Gemini isolation:
  `PASS` — malformed Gemini jobs stop before transaction start; forged target
  receipts and hostile original/staged ID reuse stop with zero cron deletion;
  committed Gemini deletion, enablement, schedule drift, and attempted re-baseline
  all fail closed, while later unrelated unknown-job changes remain allowed.
- Latest Gemini receipt selection: `10 passed`; receipt/re-entry security
  selection: `36 passed`; complete reconciliation suite: `272 passed`;
  complete Python suite: `534 passed`.
- Fresh deterministic Skill: `PASS`, 56 source files; artifact SHA-256
  `4f19357b54b21609a15092f65d882590a4f5ee370aa1e4273396a39feaa54c63`.
- Template Node suite: `28 passed`; Plugin Node suite: `5 passed`; Plugin syntax
  and official validation: `PASS`; both production dependency audits: zero
  vulnerabilities; package dry-runs, post-run checks, dangerous-exec isolation,
  Python compile, secret-pattern scan, local-only provider scan, archive parity,
  and `git diff --check`: `PASS`.

Evidence logs are under the workspace tool-run log root, principally:

- `20260907_162302_qwen-p2-reentry-graph-focused.log`
- `20260907_224948_qwen-gemini-receipt-p2-focused.log`
- `20260907_225111_qwen-gemini-receipt-p2-reconciliation-rerun.log`
- `20260907_225140_qwen-gemini-receipt-p2-full-python.log`
- `20260907_223057_qwen-repair-template-gates-latest.log`
- `20260907_223142_qwen-repair-plugin-tests-latest.log`
- `20260907_223209_qwen-repair-plugin-validate-latest.log`
- `20260907_225217_qwen-gemini-receipt-p2-check-skill.log`
- `20260907_225237_qwen-gemini-receipt-p2-diff-check.log`
- `20260907_225327_qwen-gemini-receipt-p2-local-only.log`
- `20260907_225333_qwen-gemini-receipt-p2-secret.log`

No live cron, runtime, configuration, or customer data was mutated during this
candidate update. Commit remains gated on independent review. Live integration
also remains gated on explicit Human approval and the controlled-cutover checks
below.

## Review and stop conditions

Stop on any active owned cron, shared lock owner, unknown inventory drift,
non-loopback endpoint, mismatched receipt, rollback uncertainty, or mutation
outside the exact installer-owned IDs. Implementer completion does not authorize
live deployment; PM review of diff and evidence is required first.
