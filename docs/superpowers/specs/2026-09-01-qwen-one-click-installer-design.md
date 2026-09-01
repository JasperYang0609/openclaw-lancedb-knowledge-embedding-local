# Qwen 本機 Embedding 客戶一鍵安裝器增量設計

日期：2026-09-01

狀態：`WRITTEN_PENDING_USER_REVIEW`

核准背景：Jasper 已核准採方案 A，以 llama.cpp 官方 `b10625` macOS ARM64 預編譯包完成 D5-01。本文只補齊已簽收的自動下載／續傳與單一 CLI，不授權 Production Gemini 切換。

## 一句話目標

讓已安裝 Python 3 與系統 `curl` 的 macOS Apple Silicon 客戶，以單一 CLI 完成 Qwen 模型與 llama.cpp runtime 的 preflight、可續傳下載、固定 SHA-256 驗證、安全解壓、安裝、健康 canary、狀態檢查與解除安裝。

## 方案選定

### A. llama.cpp 官方預編譯包（採用）

- 優點：不要求 Homebrew、CMake 或本機編譯；GitHub 官方 release 提供 asset digest；最接近真正一鍵安裝。
- 代價：runtime identity 從先前本機編譯 commit 改為官方 `b10625`，必須重新做 embedding parity、品質、fresh install 與故障 Gate。

### B. 固定 commit 本機編譯（不採用）

- 可保留原五日測試 runtime identity。
- 乾淨 Mac 必須另裝 CMake／編譯工具，耗時較長且失敗面更大，不符合客戶一鍵安裝目標。

### C. 安賽自行發布 binary（不採用）

- 可保留已測 binary 並縮短安裝時間。
- 會把 binary 供應鏈、授權附件、更新、簽署與長期維護責任轉移給安賽，本階段沒有必要。

## 固定 artifact identity

### Qwen model

- Repository：`Qwen/Qwen3-Embedding-4B-GGUF`。
- Revision：`f4602530db1d980e16da9d7d3a70294cf5c190be`。
- Filename：`Qwen3-Embedding-4B-Q5_K_M.gguf`。
- Bytes：`2,888,936,736`。
- SHA-256：`9fd05563211c2d69d74abb8769fa92983a102d11575b2517a119b0037dff217c`。
- URL 必須固定到上述 immutable revision；不得使用 `main`、`latest` 或未固定 redirect target。

### llama.cpp runtime

- Official release：`b10625`。
- Commit：`0cc5b14959ee3a813bd787baaef50a170493547a`。
- Asset：`llama-b10625-bin-macos-arm64.tar.gz`。
- Bytes：`10,955,118`。
- Archive SHA-256：`f13c74d104c1ff2e37a14ecb2025afe5c9c4c148064badfd8116376018dd5159`。
- 來源只允許 `https://github.com/ggml-org/llama.cpp/releases/download/b10625/` 的固定 asset URL。

## 單一 CLI

Bundled command：`python3 scripts/qwen_local.py <command>`。

- `install`：preflight → artifact download／reuse → hash verify → secure extract → atomic promote → local credential → runtime start → health＋embedding canary → terminal receipt。
- `verify`：回讀 manifest、artifact inventory、hash、permissions、platform與 runtime identity，不啟動 cloud fallback。
- `start`：啟動單一 loopback sidecar，重複執行保持 idempotent。
- `status`：輸出 redacted JSON，包含 installed／running／healthy／identity；不得輸出 credential、全文或向量。
- `stop`：只停止 manifest 綁定的受管 process。
- `uninstall`：先停止 sidecar，只移除 manifest allowlist 內的受管 runtime、model、credential、cache 與 log；遇到未知檔案、symlink 或 identity mismatch 時 fail closed。

預設 target 使用使用者資料目錄內的明確 Qwen 受管 root；可用 `--target` 覆寫，但必須通過 specificity、symlink、owner 與 Production Gemini 邊界檢查。

## 下載與續傳

- 由 Python CLI 以 argument array 呼叫系統 `curl`，不經 shell interpolation。
- 固定使用 HTTPS-only、TLS 1.2+、redirect、fail-on-HTTP-error、retry 與 connect／overall timeout。
- 下載寫入同目錄 `.part`；已存在 `.part` 時用 HTTP range 續傳。
- 完成後先驗證 bytes 與 SHA-256，再以 atomic rename 轉正；失敗保留可續傳 `.part`，但不得執行或安裝。
- 已有正式檔且 hash 正確時直接重用；正式檔 hash 錯誤時移至同一受管 cache 的 quarantine 名稱，不覆寫、不執行。
- redirect 後的有效 URL 必須仍為 HTTPS；CLI 不接受任意使用者 URL。
- log 只記錄 artifact id、bytes、phase、錯誤類型與 redacted URL host，不記錄 credential 或本機敏感路徑。

## 安全解壓與安裝

- runtime archive hash 通過後才可讀取。
- 拒絕 absolute path、`..` traversal、symlink、hardlink、device、FIFO 與非預期頂層目錄。
- 只接受固定 `llama-b10625/` inventory；必須包含 `llama-server`、LICENSE 與其 runtime dylibs。
- 解壓到同檔案系統 staging directory；每個 regular file 記錄相對路徑、bytes、SHA-256 與 executable bit。
- 完成 inventory／Mach-O architecture／`llama-server --version` 驗證後才 atomic promote。
- manifest 綁定 archive digest、release commit、完整 inventory、model identity、platform、install root 與 schema version。
- 不修改 PATH、shell rc、LaunchAgent、OpenClaw Production config、Gemini cache／DB 或現行排程。

## 錯誤與回復

- 網路中斷：保留 `.part`，下次 `install` 從既有 bytes 續傳。
- Server 不支援 range 或回傳不一致：安全重啟該 artifact 下載；不把重複 bytes append 成成功檔。
- hash／size mismatch：artifact 標為 quarantined，安裝停止。
- staging crash：既有已驗證 installation 不變；下次清理同 manifest 綁定的 stale staging 後重試。
- canary 失敗：停止 sidecar，installation 標 `installed_unhealthy`；不得切換 provider。
- 重複 install：identity 相同且 verify PASS 時 no-op；identity 不同時停止並要求明確 upgrade 流程。

## 驗證 Gate

- Unit：URL allowlist、curl argument array、resume、range fallback、bytes／hash、atomic rename、quarantine、secure tar、manifest、permissions、idempotency、unknown files、symlink與 target boundary。
- Integration：本機 HTTP fixture 模擬完整下載、中斷續傳、忽略 Range、redirect、404、timeout、tamper與 checksum mismatch。
- Artifact：實際下載官方 10.4 MiB runtime；模型完整 2.69 GiB 以既有官方 hash 檔重用做 install，並另以小型 fixture 驗證 downloader，不重複浪費頻寬。
- Runtime：官方 `b10625` 真實 start／health／embedding canary、stop／restart、單一 listener、離線查詢。
- Parity：同一批固定文本與查詢比較原 runtime／`b10625` 的 2,560 維及截斷 768 維向量；記錄 cosine、norm、finite與 deterministic repeat。
- Quality：重跑 20 題 Qwen benchmark；Hit@5 不低於 85%、MRR 不低於 0.7167、端到端 p95 不高於 1 秒。
- Packaging：fresh isolated bootstrap、單一 CLI install／verify／status／start／stop／uninstall、skill archive parity、秘密掃描與 GitHub CI。

若 runtime parity 或品質 Gate 未通過，方案 A 停止，不以「較方便安裝」降低先前品質與安全標準。

## OWASP Top 10:2025 增量 Gate

- A01：target／uninstall allowlist、Production Gemini negative test。
- A02：HTTPS-only、loopback-only、no cloud fallback、restricted files。
- A03：immutable revision、official release digest、artifact inventory、LICENSE、dependency audit。
- A04：credential 0600、不進 argv／log／manifest value。
- A05：URL／path／tar member／CLI input allowlist，subprocess 不使用 shell。
- A06：idempotent install、atomic promote、resource／disk／timeout limits、no automatic provider cutover。
- A07：`NOT_APPLICABLE_WITH_EVIDENCE`；沒有登入／session。
- A08：archive＋file hashes、manifest identity、corruption與 rollback tests。
- A09：redacted phase／receipt／failure telemetry，不記錄 corpus、vectors或秘密。
- A10：network interruption、range anomaly、timeout、tamper、partial success、stale staging與cleanup fault tests。

ASVS 仍為 `NOT_APPLICABLE_WITH_EVIDENCE`；本功能是本機 CLI，無公開 Web／API。

## 不在本輪範圍

- 不切換現行 Production Gemini。
- 不自動建立或重建正式 96,163-row Production index。
- 不支援 Windows、Linux 或 Intel Mac。
- 不新增 telemetry、cloud fallback、LaunchAgent或背景自動更新。
- 不使用 `latest`、遠端動態 manifest 或未經 Human Gate 的自動 runtime upgrade。

## 完成定義

只有全部本機／GitHub Gate 通過、P0／P1 為 0、skill archive 重建並有 commit hash，才可標記 `CUSTOMER_INSTALLER_CANDIDATE_READY`。正式客戶發佈與 Production provider cutover仍需各自獨立 Human Gate。
