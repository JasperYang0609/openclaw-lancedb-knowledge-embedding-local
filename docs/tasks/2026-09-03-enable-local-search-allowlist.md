# Enable local knowledge search in the production tool policy

Date: 2026-09-03

Status: `IN_PROGRESS`

## Objective

Allow the already-installed read-only `local_knowledge_search` tool in the current OpenClaw profile, restart the Gateway safely, and complete the approved five source-dependent plus two general-knowledge conversation checks.

## Scope and boundaries

- Preserve every existing tool permission and add only `local_knowledge_search` to the active explicit allowlist.
- Preserve the ready Qwen index, local-only sidecar, incremental schedule, Skill, Plugin, and disabled Gemini schedule.
- Back up the validated OpenClaw configuration before changing it.
- Roll back the configuration if validation, Gateway restart, tool ownership, direct search, or conversation checks fail.
- Do not enable Gemini, delete rollback assets, expose corpus content, or broaden any other tool permission.

## Acceptance criteria

- Installer regression tests select `tools.allow` when present and `tools.alsoAllow` when it is the active explicit policy.
- OpenClaw configuration validates before and after the change.
- The Plugin is loaded and owns exactly the read-only `local_knowledge_search` contract.
- Gateway RPC, Qwen loopback health, READY index, and unique incremental schedule pass.
- Five source-dependent fresh-session prompts invoke local search and cite sources; two general-knowledge controls do not invoke local search.
- Repository tests, dependency audit, secret scan, diff review, commit, CI, and clean-worktree checks pass.

## OWASP Top 10:2025 gate

- A01: exact allowlist merge; no unrelated permission changes.
- A02: validated config, loopback-only service, no cloud fallback.
- A03: pinned dependencies and audit evidence.
- A04: no credentials written to Git or logs.
- A05: closed search schema and fixed-argv execution remain unchanged.
- A06: backup, rollback, idempotency, and fail-closed checks.
- A07: no authentication or session model change; evidence recorded as not applicable.
- A08: config hash, Plugin ownership, index readiness, and repository integrity checks.
- A09: redacted operational evidence only; no prompt or corpus bodies in committed logs.
- A10: restart and verification failures trigger rollback; no partial success claim.
