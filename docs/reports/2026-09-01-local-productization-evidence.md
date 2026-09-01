# Qwen local repository productization evidence

Status: CUSTOMER_INSTALLER_CODE_READY; LIVE_ARTIFACT_REHEARSAL_BLOCKED

## Passed

- Python unit/integration: 26 passed.
- Node template: 27 passed.
- Bootstrap, snapshot, cron tooling, dangerous-exec, postrun and deterministic archive parity: PASS.
- npm production dependency audit: 0 vulnerabilities.
- Runtime source negative scan: no cloud embedding endpoint, cloud credential read, cross-provider branch, local-hash product provider or shadow-index entry.
- Secret-pattern and tracked/untracked file-size scans: PASS; model/corpus/vector/credential artifacts are absent.
- Unified CLI uninstalled `status` smoke: PASS with redacted JSON.

Full logs: `20260901_124042_local-qwen-final-precommit.log`, `20260901_123740_local-qwen-final-scans-r2.log` under the workspace tool-run log root. The deterministic archive contains 41 files, including the unified CLI, installer, lifecycle manager and dependency manifest.

## Blocked live-only evidence

Spotlight found no existing verified `Qwen3-Embedding-4B-Q5_K_M.gguf` or `llama-b10625-bin-macos-arm64.tar.gz` artifact. Per scope, this run did not download the 2.69 GiB model. Therefore the real b10625 start/embedding/stop, two fresh live install/uninstall rounds, offline query, vector parity and 20-query quality/latency rerun remain blocked on an already-downloaded verified model/runtime artifact.

Fixture coverage for downloader resume/fallback/tamper, safe extraction, installer integrity, lifecycle identity, uninstall boundaries and CLI redaction is complete. This report does not claim a production release, GitHub CI result, PR, merge or Production cutover.
