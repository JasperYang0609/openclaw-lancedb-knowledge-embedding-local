# Qwen local repository OWASP Top 10:2025 gate

- A01 PASS — target specificity, owner/symlink checks, manifest-scoped uninstall and protected-root negative tests.
- A02 PASS — loopback-only runtime, Web UI disabled, restricted state files, provider negative scan.
- A03 PASS — immutable Qwen and llama.cpp revisions, byte counts, SHA-256, fixed hosts and license inventory requirement.
- A04 PASS — CSPRNG credential, mode 0600, credential value excluded from manifest/status/logs.
- A05 PASS — subprocess argument arrays with `shell=False`; URL, CLI, path and tar-member validation.
- A06 PASS — atomic artifact promotion, staging extraction, idempotent start/install identity, no automatic provider cutover.
- A07 NOT_APPLICABLE_WITH_EVIDENCE — no remote login or session; local bearer lifecycle is covered by A02/A04.
- A08 PASS — artifact and extracted-file hashes, schema-v2 manifest identity, corruption and unknown-file failure tests.
- A09 PASS — CLI emits redacted phase/status/error categories; no corpus, vectors or credentials.
- A10 PASS — partial download retention, range fallback, checksum failure, stale PID, port collision, partial staging and cleanup failure behavior.

ASVS: NOT_APPLICABLE_WITH_EVIDENCE. This is a local CLI and loopback sidecar, not a public Web/API authentication surface.

Attacker review: path injection, archive traversal/links/devices, manifest forgery, PID reuse, download tamper, unsafe uninstall and cross-provider configuration are fail-closed. Live 2.69 GiB model rehearsal remains separately evidenced because the repository must not download or commit the model during fixture verification.
