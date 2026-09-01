# Qwen local repository productization evidence

Status: CUSTOMER_INSTALLER_AND_LIVE_ARTIFACT_REHEARSAL_PASS

## Passed

- Python unit/integration: 52 passed.
- Node template: 28 passed.
- Bootstrap, snapshot, cron tooling, dangerous-exec, postrun and deterministic archive parity: PASS.
- npm production dependency audit: 0 vulnerabilities.
- Runtime source negative scan: no cloud embedding endpoint, cloud credential read, cross-provider branch, local-hash product provider or shadow-index entry.
- Secret-pattern, packaged-archive and tracked-file-size scans: PASS; model/corpus/vector/credential artifacts are absent.
- Unified CLI uninstalled `status` smoke: PASS with redacted JSON.
- Isolated-Python probe (`-I`) passed without user-site packages; the customer runtime now uses only the Python standard library.
- The deterministic skill archive includes the repository MIT `LICENSE`.

Final resumed regression logs: `20260901_134707_local-qwen-full-regression-after-resume.log` and `20260901_135009_local-qwen-security-boundary-scans-resumed-r4.log` under the workspace tool-run log root. The deterministic archive contains 42 files, including the unified CLI, installer, lifecycle manager, dependency manifest and MIT license. GitHub Actions are pinned to immutable commit SHAs.

## Live official-artifact evidence

- Reused an existing verified Qwen Q5 model with the pinned model SHA-256; the model was not copied into Git.
- Downloaded the official immutable `b10625` macOS arm64 archive through the customer CLI. The downloaded byte count and SHA-256 matched the pinned release metadata.
- The downloader validates the final HTTPS host after redirects against explicit official CDN hosts, enforces an artifact-size ceiling, and refuses unapproved redirect destinations before promotion.
- A live official-runtime resume test restarted from a 1 MiB partial file and completed with the exact pinned byte count and SHA-256.
- Two fresh isolated install/verify/health/stop/uninstall rounds passed. Both uninstall rehearsals removed only their managed roots and left the Production Gemini installation untouched.
- The official archive contains bounded same-directory dynamic-library symlinks. The extractor now validates the complete chain and materializes aliases as ordinary files; absolute, escaping, looping, missing-target and non-regular links remain rejected. The installed runtime contains no symlinks.
- The official executable must remain beside its dynamic libraries. Installation now verifies the runtime in a staging directory and only atomically promotes it after the exact build/commit/platform marker passes.
- The pinned `b10625` runtime produced a finite 2560-dimensional native vector and a unit-normalized 768-dimensional product vector. Compared with the previously validated runtime, cosine similarity was `1.000000000000` and maximum absolute difference was `0`.
- A fresh 20-query full-index rerun against the new runtime passed at Hit@5 `18/20` (`90%`) and MRR `0.7667`, identical to the signed Day 5 baseline. The local-only search path completed without a cloud provider or cloud fallback configuration.
- The same 20-query end-to-end path measured fresh CLI process, query embedding, LanceDB retrieval and output rendering at p50 `526.8 ms`, p95 `565.7 ms`, mean `539.0 ms` and max `824.9 ms`; the signed p95 target of `1,000 ms` passed.
- Production installation now rejects pre-planted target, model-directory and model-file symlinks before copying the model. An isolated negative test confirmed an outside user-owned file was unchanged.
- When the sidecar port is live but the managed PID record is missing, uninstall fails closed and preserves the runtime. The live negative rehearsal passed before a restored managed stop/uninstall removed the isolated target.
- The index fingerprint now binds the query instruction, runtime release, immutable runtime commit and runtime archive SHA-256 in addition to model identity.

Live logs: `20260901_125133_qwen-local-live-install-round1-r6.log`, `20260901_125307_qwen-local-live-install-round2.log`, `20260901_125452_qwen-local-live-round2-verify-uninstall.log`, `20260901_125625_qwen-local-runtime-benchmark-install.log`, `20260901_125716_qwen-runtime-vector-parity.log`, `20260901_125837_qwen-b10625-full-20-query-benchmark.log`, `20260901_131739_qwen-clean-python-live-install-after-review.log`, `20260901_131847_qwen-b10625-20-query-e2e-latency-p95.log`, `20260901_131923_qwen-orphan-uninstall-live-negative.log`, and `20260901_135039_local-qwen-official-runtime-resume-live.log` under the workspace tool-run log root.

This report does not claim a Production cutover. PR, CI and merge evidence are recorded separately during repository closeout.
