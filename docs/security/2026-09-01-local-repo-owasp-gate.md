# Qwen local repository OWASP Top 10:2025 gate

- A01 PASS — target specificity, owner/symlink checks, manifest-scoped uninstall and protected-root negative tests.
- A02 PASS — loopback-only runtime, Web UI disabled, restricted state files, provider negative scan.
- A03 PASS — immutable Qwen and llama.cpp revisions, byte counts, SHA-256, fixed hosts, exact runtime build/commit/platform verification and license inventory requirement; official artifacts passed live verification.
- A04 PASS — CSPRNG credential, mode 0600, credential value excluded from manifest/status/logs.
- A05 PASS — subprocess argument arrays with `shell=False`; URL, CLI, path and tar-member validation. Official same-directory dylib symlink chains are validated and materialized as regular files; traversal, loops, missing targets, cross-directory targets, hardlinks and special files fail closed.
- A06 PASS — atomic artifact promotion, staging extraction and runtime verification, idempotent start/install identity, no automatic provider cutover. Two fresh live install/uninstall rounds passed.
- A07 NOT_APPLICABLE_WITH_EVIDENCE — no remote login or session; local bearer lifecycle is covered by A02/A04.
- A08 PASS — artifact and extracted-file hashes, schema-v2 manifest identity, corruption and unknown-file failure tests; official runtime and model hashes passed live readback.
- A09 PASS — CLI emits redacted phase/status/error categories; no corpus, vectors or credentials.
- A10 PASS — partial download retention, range fallback, checksum failure, stale PID, port collision, partial staging and cleanup failure behavior.

ASVS: NOT_APPLICABLE_WITH_EVIDENCE. This is a local CLI and loopback sidecar, not a public Web/API authentication surface.

Attacker review: path injection, archive traversal/escaping or cyclic links/devices, manifest forgery, PID reuse, download tamper, unsafe uninstall and cross-provider configuration are fail-closed. The 2.69 GiB model remains outside Git; live verification used an existing hash-matched artifact. Two fresh install/uninstall rounds, vector parity and the 20-query full-index gate passed without modifying Production Gemini.
