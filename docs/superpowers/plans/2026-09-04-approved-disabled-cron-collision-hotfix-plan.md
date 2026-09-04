# Approved Disabled Cron Collision Hotfix Plan

1. Add failing reconciliation and CLI tests for the three-field approval contract,
   exact legacy incremental shape, negative tamper cases, transaction receipt,
   rollback preservation, and idempotence.
2. Add constructor and CLI parsing/resolution for the ID, ID-inclusive SHA-256, and
   `incremental` role; reuse safe approval metadata from a private committed receipt.
3. Exempt only the exact approved job from the incremental collision blocker while
   keeping it in unknown-inventory hashes and all rollback/readback checks.
4. Update README, Skill, security gate, post-run/package checks, and rebuild the
   deterministic Skill archive.
5. Run focused and full Python/Node suites, archive parity, plugin validation,
   dependency/security scans, and independent exact-commit review.
