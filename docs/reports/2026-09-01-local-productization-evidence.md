# Qwen local repository productization evidence

Status: CUSTOMER_INSTALLER_AND_LIVE_ARTIFACT_REHEARSAL_PASS

## Passed

- Python unit/integration: 32 passed after the live-artifact fixes.
- Node template: 27 passed.
- Bootstrap, snapshot, cron tooling, dangerous-exec, postrun and deterministic archive parity: PASS.
- npm production dependency audit: 0 vulnerabilities.
- Runtime source negative scan: no cloud embedding endpoint, cloud credential read, cross-provider branch, local-hash product provider or shadow-index entry.
- Secret-pattern and tracked/untracked file-size scans: PASS; model/corpus/vector/credential artifacts are absent.
- Unified CLI uninstalled `status` smoke: PASS with redacted JSON.

Final regression logs: `20260901_130050_local-qwen-final-regression-after-live.log` and `20260901_130124_local-qwen-final-security-scans-after-live.log` under the workspace tool-run log root. The deterministic archive contains 41 files, including the unified CLI, installer, lifecycle manager and dependency manifest.

## Live official-artifact evidence

- Reused an existing verified Qwen Q5 model with the pinned model SHA-256; the model was not copied into Git.
- Downloaded the official immutable `b10625` macOS arm64 archive through the customer CLI. The downloaded byte count and SHA-256 matched the pinned release metadata.
- Two fresh isolated install/verify/health/stop/uninstall rounds passed. Both uninstall rehearsals removed only their managed roots and left the Production Gemini installation untouched.
- The official archive contains bounded same-directory dynamic-library symlinks. The extractor now validates the complete chain and materializes aliases as ordinary files; absolute, escaping, looping, missing-target and non-regular links remain rejected. The installed runtime contains no symlinks.
- The official executable must remain beside its dynamic libraries. Installation now verifies the runtime in a staging directory and only atomically promotes it after the exact build/commit/platform marker passes.
- The pinned `b10625` runtime produced a finite 2560-dimensional native vector and a unit-normalized 768-dimensional product vector. Compared with the previously validated runtime, cosine similarity was `1.000000000000` and maximum absolute difference was `0`.
- A fresh 20-query full-index rerun against the new runtime passed at Hit@5 `18/20` (`90%`) and MRR `0.7667`, identical to the signed Day 5 baseline. The local-only search path completed without a cloud provider or cloud fallback configuration.

Live logs: `20260901_125133_qwen-local-live-install-round1-r6.log`, `20260901_125307_qwen-local-live-install-round2.log`, `20260901_125452_qwen-local-live-round2-verify-uninstall.log`, `20260901_125625_qwen-local-runtime-benchmark-install.log`, `20260901_125716_qwen-runtime-vector-parity.log`, and `20260901_125837_qwen-b10625-full-20-query-benchmark.log` under the workspace tool-run log root.

This report does not claim a Production cutover. PR, CI and merge evidence are recorded separately during repository closeout.
