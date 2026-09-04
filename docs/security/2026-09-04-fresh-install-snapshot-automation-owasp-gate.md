# Qwen local fresh-install snapshot automation security gate

Date: 2026-09-04
Status: `IMPLEMENTER_PASS_PENDING_INDEPENDENT_REVIEW`
Scope: local CLI, OpenClaw cron reconciliation, installer-owned runtime files,
immutable Qwen-local recovery snapshots, and the bounded backup-health receipt.

## Trust boundaries and protected data

- Trusted identities: exact declaration keys, installer ownership manifest, private
  transaction state, pinned package files, and the loopback Qwen provider identity.
- Untrusted inputs: CLI paths/options, complete cron inventory JSON, unknown cron
  jobs, filesystem contents, snapshot manifests, and source/index closeout files.
- Protected data: local index/corpus/cache, snapshots, OpenClaw configuration,
  existing customer jobs, Gemini rollback assets, and alert routing.
- Abuse cases: declaration collision, partial inventory, concurrent index/snapshot
  mutation, symlink/hardlink/special-file substitution, stale or tampered snapshot,
  secret-bearing rollback receipt, forged/stale disabled-collision approval, failed
  restoration, and cloud fallback.

## OWASP Top 10:2025 closeout

- A01 Broken Access Control — `PASS`. Absolute contained roots, current-user
  ownership, exact declaration identity, unknown-job collision blocking, exact
  operator ID plus ID-inclusive SHA-256 migration, committed-receipt-only approval
  reuse, and no mutation of unknown jobs are covered by reconciliation, path,
  symlink, hardlink, transaction, and rollback tests.
- A02 Security Misconfiguration — `PASS`. Fresh install requires explicit Discord
  provider/channel/account routing; recurring jobs require exact description,
  isolated session, fixed schedule/argv/cwd/limits, delivery `none`, first-failure
  announce alert, and no tools. Qwen endpoint remains loopback HTTP with no fallback.
- A03 Software Supply Chain Failures — `PASS`. The deterministic 55-file Skill
  archive matches source, package lockfiles are retained, Plugin validation passes,
  and offline production dependency audits report zero vulnerabilities for both
  shipped Node packages. Online registry audit was unavailable within the bounded
  validation window and did not weaken the pinned/offline gate.
- A04 Cryptographic Failures — `PASS`. Snapshot assets, manifests, rollback config,
  archive contents, operator-selected legacy jobs, and health receipts use SHA-256
  or exact byte/readback checks. Disabled-collision approval hashes the complete
  normalized contract including the exact job ID, preventing identity substitution.
  No credential, token, corpus, query, vector, argv, environment, or path is stored
  in the approval receipt.
- A05 Injection — `PASS`. Managed work uses fixed argv and `shell=False`; cron
  definition fields, timezones, account IDs, channel IDs, paths, names, and payload
  schemas are bounded. Retrieved/model content is never used to construct commands.
- A06 Insecure Design — `PASS`. Write-ahead transaction state precedes quiescence;
  managed and exact Gemini jobs are disabled and allowed to finish before runtime
  mutation; independent index and snapshot locks cover replacement. Snapshot runs
  atomically acquire the same directory lock used by index writers and hold it for
  the complete copy, verification, receipt, and retention transaction, closing the
  absence-check TOCTOU window in both directions. Recurring jobs enable last only
  after both disabled contracts pass global readback. Daily snapshots are immutable
  and stale state creates a distinct repair snapshot. A typed rollback-incomplete
  outcome prevents the CLI from restarting a prior manual runtime into uncertain
  launchd/runtime state; restart failure after verified rollback preserves both
  failure types. The sole disabled-collision exception is a closed incremental-only
  contract and remains unknown inventory throughout install and rollback.
- A07 Authentication Failures — `NOT_APPLICABLE_WITH_EVIDENCE`. This change adds no
  public endpoint, login, session, or credential store. Existing authenticated
  OpenClaw control-plane access is unchanged; local ownership is covered by A01/A02.
- A08 Software or Data Integrity Failures — `PASS`. Snapshot verification checks the
  exact file set, SHA-256, freshness, isolated restore, LanceDB open, table identity,
  row count, immutability, and retention ordering. Receipt reads use bounded
  no-follow descriptors with pre/post identity checks. Rollback verifies restored
  owned definitions and unchanged unknown-job hashes globally, including the exact
  approved disabled job ID and ID-inclusive hash.
- A09 Security Logging and Alerting Failures — `PASS`. Incremental, snapshot, and
  pending initial jobs have explicit first-failure announce alerts. The component
  receipt has exact producer/declaration/freshness schemas, private permissions,
  bounded size/items, string-valued data-loss state, and redacted summaries. Index
  wrappers emit an error receipt for unsafe lock nodes, permissions, and non-EEXIST
  creation failures; only a verified owner-safe directory is a normal contention skip.
- A10 Mishandling of Exceptional Conditions — `PASS`. Tests cover incomplete cron
  inventory, active-job wait, index/snapshot lock timeout, status-write failure,
  cron remove/enable failure, unconditional rollback, stale repair generations,
  database timeout, checksum/tamper, unsafe nodes, failed fresh-install cleanup,
  unapproved collisions, incomplete approvals, ID/hash/role drift, enabled toggles,
  declaration appearance, duplicate IDs, contract tamper, non-committed approval
  receipts, fresh/upgrade idempotence, and exact fault rollback preservation.

## Verification evidence

- Python suite: final hotfix closeout rerun `258 passed`.
- Focused reconciliation/CLI hotfix suite: `88 passed`.
- Qwen template Node suite: `28 passed`.
- OpenClaw Plugin Node suite: `5 passed`; syntax check and official Plugin validation
  passed.
- Production dependency audit: `0 vulnerabilities` for both shipped packages in
  offline mode.
- Skill archive: deterministic parity passed with 55 source files and packaged CLI
  smoke passed.
- Secret-pattern scan: no GitHub/OpenAI/Google/AWS/JWT-shaped credential found.
- Shell syntax: passed; `shellcheck` was unavailable, so no shellcheck claim is made.

## Attacker review and release gate

The attacker-oriented review covered unauthorized cron adoption, declaration drift,
inventory truncation, secret-bearing env/receipts, concurrent runtime replacement,
snapshot path/file substitution, same-day rollback attacks, immutable snapshot
overwrite, retention escape, cloud fallback, and rollback failure. Independent
review identified three P1s: an index/snapshot absence-check TOCTOU gap, unsafe index
lock creation failures misreported as normal contention, and unconditional manual
runtime restart after an unverified rollback. The shared atomic lock protocol,
owner/type/permission-aware lock helper, typed recovery outcomes, and adversarial
regressions close them. No known P0/P1 remains, but independent review of the new
exact commit remains mandatory before merge/release.

`ASVS_LEVEL_TARGET`: `NOT_APPLICABLE_WITH_EVIDENCE`. There is no Web application or
public product API in this change. Equivalent local CLI/filesystem/process controls
are enumerated above.
