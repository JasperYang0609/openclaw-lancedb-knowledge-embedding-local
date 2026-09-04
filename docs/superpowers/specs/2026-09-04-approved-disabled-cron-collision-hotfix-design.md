# Approved Disabled Cron Collision Hotfix

## Context

Fresh-install preflight correctly blocks any unknown cron job that targets an
installer-owned Qwen wrapper. A disabled pre-declaration Qwen incremental job can
therefore block a safe upgrade even when an operator has independently verified it
and wants it retained as rollback evidence.

## Decision

Add one narrow, operator-approved exception with three mandatory values:

- exact cron job ID;
- SHA-256 of the complete normalized cron contract, including that ID;
- exact role `incremental`.

The exception is accepted only when the inventory contains that ID exactly once,
the job is explicitly disabled, its declaration key is absent or empty, and every
managed contract field matches the one supported legacy incremental shape. The
shape has an exact name, schedule, isolated session, shell argv, timeout, delivery,
alert state, and no cwd, environment, tools, or extra payload fields. Snapshot jobs
and alternative shells or argv are never approved by this mechanism.

The approved job remains unknown inventory. The installer never edits, disables,
enables, removes, or adopts it. Its ID-inclusive hash participates in the same
preflight-to-quiescence drift check, transaction receipt, activation readback,
idempotent verification, and rollback preservation check as every other unknown
job. A later ID, hash, enabled-state, declaration-key, role, or contract change
fails closed before installer mutation.

Only `{jobId, contractSha256, role}` may be persisted in the private ownership
receipt. Raw argv, environment, paths, and cron payloads are not added to approval
metadata. A subsequent CLI invocation may reuse that private stored approval; an
explicit CLI approval overrides it only when all three values are supplied. Stored
approval reuse additionally requires the surrounding transaction receipt to have
exact integer `schemaVersion: 1` and exact `phase: committed`; prepared, activation
pending, failed, rollback-failed, rolled-back, malformed, and wrong-version receipts
never authorize a collision. Recovery commands ignore this optional approval field.

## Operator flow

Without approval, preflight continues to block and reports only the safe job ID,
role, and canonical ID-inclusive SHA-256 needed for review. The operator must use
the fingerprint printed by the current-version diagnostic, not an older hash made
without the job ID. After independent review, the
operator reruns with:

- `--approve-disabled-collision-job-id`
- `--approve-disabled-collision-job-sha256`
- `--approve-disabled-collision-role incremental`

Partial approvals and malformed hashes are rejected. Unknown look-alikes remain
blocked.

## Alternatives considered

- Automatically ignore any disabled collision: rejected because disabled jobs can
  still be tampered with or later enabled.
- Approve by hash only: rejected because the same contract could be copied to a new
  identity after review.
- Delete or adopt the old job: rejected because it violates rollback preservation
  and expands installer ownership.

## Verification

Tests cover fresh install and committed upgrade, missing or duplicate ID, wrong
hash, enabled toggle, declaration-key appearance, wrong role, snapshot collision,
extra argv/shell/payload fields, safe receipt metadata, preflight drift, exact
unknown preservation, rollback, and identical idempotent reruns. Existing default
collision tests must remain fail-closed.
