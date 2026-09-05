# Command cron tools CLI compatibility repair

## Context

The live OpenClaw 2026.7.1-2 CLI rejects `cron edit <command-job> --clear-tools` by
building an invalid `agentTurn` patch. The Qwen transaction correctly rolled back,
but this post-create edit prevents the two owned command jobs from committing.

## Goal

Keep the approved 06:30 incremental and 06:50 verified-snapshot contracts unchanged.
Fresh command jobs already omit `payload.toolsAllow`, so their alert edit must not
send a tool-list patch. The live CLI also ignores command tool lists during create,
so a legacy command definition containing that field must fail closed pre-mutation.

## Scope

- Remove the unsupported command-job `--clear-tools` edit.
- Reject command `toolsAllow` in owned, legacy-upgrade, and rollback definitions before
  any cron mutation; rollback must never emit `--tools`.
- Correct the packaged cron-tooling auditor so only agentTurn jobs recommend
  `--clear-tools`; command jobs require reviewed recreation.
- Add a regression test that asserts the compatible edit contract.
- Rebuild the deterministic Skill artifact.
- Do not change indexes, snapshots, source maps, schedules, customer jobs, or data.

## Acceptance

- Both owned command jobs stage, configure alerts, enable, and verify exactly.
- `payload.toolsAllow` is absent on live readback.
- Interrupted transaction reconciliation reaches committed or verified rollback.
- Full Python/Node/package/security checks and a live disabled canary pass.

## Security gate

- A01/A07: exact owned declaration keys and collision approval remain unchanged.
- A02/A08: SHA-256 receipts, asset identity, and package parity are reverified.
- A03/A05: fixed argv; no shell interpolation or new configuration surface.
- A04/A06/A09/A10: transaction rollback, dependency audit, bounded diagnostics, and
  local-only endpoint checks remain required. No new AI or network trust boundary.
- Authentication is not applicable; existing local Gateway authorization is reused.

## Stop conditions

Stop and roll back on unknown cron drift, non-loopback Qwen configuration, receipt
failure, or any mutation outside the two owned jobs and versioned managed assets.
