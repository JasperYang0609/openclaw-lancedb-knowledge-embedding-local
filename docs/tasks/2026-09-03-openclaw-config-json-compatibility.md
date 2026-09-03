# OpenClaw config JSON compatibility repair

Date: 2026-09-03

Status: `IMPLEMENTING_APPROVED`

## Objective

Repair `integrate-openclaw` on OpenClaw `2026.7.1-2` by obtaining the active configuration path from the official machine-readable `openclaw config validate --json` response instead of interpreting the human-oriented `openclaw config file` output as a bare path.

## Authorized scope

- Replace only the active-config discovery implementation.
- Preserve the existing managed-home boundary and symbolic-link rejection.
- Add owner, regular-file, single-link and private-permission validation before snapshot or mutation.
- Run isolated regression tests, the full repository gates and a production-profile read-only path canary.
- After review and CI pass, resume the already-approved transactional OpenClaw integration and acceptance flow.

## Out of scope

- No index rebuild or vector mutation.
- No Gemini reactivation, deletion or cloud fallback.
- No weakening of path, ownership, permission or rollback controls.
- No unrelated OpenClaw configuration changes.

## Acceptance criteria

- Notices on stderr do not affect path discovery.
- `valid` must be exactly `true`; `path` must be a non-empty absolute string.
- The path must be a specific child of the managed home and contain no symbolic-link component.
- The config must be a regular file owned by the current user, have exactly one hard link and grant no group/world permissions.
- Invalid JSON/schema, relative/outside paths, broad permissions, symlinks and hardlinks fail closed.
- Full local and GitHub CI gates pass; production integration remains transactional and rollback-capable.

## OWASP Top 10:2025 evidence plan

- A01: managed-home boundary, current-user ownership and exact config identity tests.
- A02: private permission and config validation checks.
- A03: no dependency or artifact change; dependency audit remains required.
- A04: config contents remain in the restricted local snapshot and are never logged.
- A05: strict JSON schema plus absolute-path and link validation.
- A06: fail closed before mutation on any discovery or validation error.
- A07: `NOT_APPLICABLE_WITH_EVIDENCE`; no authentication/session behavior changes.
- A08: pre-change config SHA-256 and transaction manifest remain unchanged.
- A09: machine-readable errors only; no config contents or secrets in output.
- A10: malformed output and filesystem-race-adjacent link cases are covered by negative tests.

## Completion evidence

- Targeted and full test results.
- Read-only production-profile config-path canary.
- Diff review, secret scan, commit hash, PR/CI and clean worktree.
- Post-integration Plugin, Skill, Gateway, cron and 5+2 recall acceptance evidence.
