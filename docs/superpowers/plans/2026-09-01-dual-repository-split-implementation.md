# Gemini 與地端 Embedding 雙 Repository 實作計畫

日期：2026-09-01

設計依據：`docs/superpowers/specs/2026-09-01-dual-repository-split-design.md`

來源 exact commit：`f67520aa1307663097b063935cac752f6ca210ed`

狀態：`IMPLEMENTATION_PLAN_READY`

## 完成目標

交付兩個公開且產品邊界互斥的 repository：

- `JasperYang0609/openclaw-lancedb-knowledge-skill`：Gemini `embedding-001` 版。
- `JasperYang0609/openclaw-lancedb-knowledge-embedding-local`：Qwen3-Embedding-4B Q5_K_M 地端版。

地端版必須補齊單一 customer-facing CLI、自動下載／續傳、安全解壓、安裝／生命週期／解除安裝；Gemini 版保持既有 URL 與功能相容。Production 不切換。

## 執行原則

- 全程 Git-first；兩個 repository 都使用 feature branch、PR、CI 與 merge commit。
- 不 force-push、不重寫公開歷史、不刪除既有 repository。
- 新地端 repository 先完成並通過 Gate；Gemini repo 的 Qwen 移除後完成。
- 完整工具輸出寫入 `logs/tool-runs/`，聊天只回報 exit code、log path 與必要摘要。
- 實作者與 reviewer 分離；最終 diff 與證據由獨立 Codex reviewer 複核。
- 任一 P0／P1、秘密、artifact identity 不一致、路徑邊界失敗或 CI 失敗即停止發布。

## Phase 0：Preflight 與來源凍結

### 0.1 Git／GitHub preflight

- 確認既有 repository branch、HEAD、remote、default branch、visibility 與 worktree clean。
- 回讀 GitHub 上新 repository 名稱尚未占用。
- 保存來源 commit、既有 main commit、PR #3 merge commit 與 branch ancestry 到 closeout evidence。

### 0.2 任務與安全文件

建立：

- `docs/tasks/2026-09-01-local-repo-productization.md`
- `docs/tasks/2026-09-01-gemini-repo-cleanup.md`
- `docs/security/2026-09-01-dual-repo-owasp-gate.md`

每份 task spec 列出 scope、禁止事項、changed files、tests、commit、stop conditions；安全 Gate 逐項記錄 A01–A10 的 `PASS／BLOCKED／NOT_APPLICABLE_WITH_EVIDENCE`。

## Phase 1：建立地端 repository lineage

### 1.1 本機隔離 clone

- 從 exact commit `f67520a` 建立獨立 clone：`repos/openclaw-lancedb-knowledge-embedding-local`。
- 原 Gemini remote 改名為唯讀 `upstream-gemini`；新 repo 使用獨立 `origin`。
- 建立 `feat/local-one-click-installer-20260901` branch。
- 驗證兩個 working tree、`.git`、remote、branch 與 index 完全獨立。

### 1.2 建立公開 GitHub repository

- 建立空的 public repo `JasperYang0609/openclaw-lancedb-knowledge-embedding-local`。
- description 明確標示 Qwen local embedding、macOS Apple Silicon、no cloud embedding fallback。
- 不在 Gate 通過前發布 release 或宣稱 production-ready。

## Phase 2：地端 repository 產品邊界清理

### 2.1 唯一 skill identity

重新命名並更新：

- `openclaw-lancedb-knowledge/` → `openclaw-lancedb-knowledge-local/`
- `SKILL.md` frontmatter name → `openclaw-lancedb-knowledge-local`
- `dist/openclaw-lancedb-knowledge.skill` → `dist/openclaw-lancedb-knowledge-local.skill`
- `scripts/build_skill_archive.py`
- `tests/test_skill_archive.py`
- `.github/workflows/ci.yml`

目的：兩版可被辨識且不因相同 skill id／archive name 互相覆蓋。

### 2.2 移除 Gemini runtime

刪除或改寫：

- 移除 template `src/embed-google.js`。
- template `src/cli.js` 只接受 `qwen-local`；deterministic mock 僅存在 tests，不作產品 provider。
- bootstrap 移除 `--google-gemini`、`--approved-by`、Gemini profile 與 Gemini cache 路徑。
- source-map example 預設 `qwen-local`、768 維、固定 fingerprint 與獨立 table/cache/state。
- 移除 Gemini runtime tests；Qwen/Gemini 歷史比較報告保留並加「historical benchmark, no runtime dependency」標示。
- README／SKILL／architecture 更新為 local-only，並連結 Gemini repo。

新增 negative scan：production source、config、bootstrap、archive 中不得出現 Google API endpoint、Gemini API key 讀取或 cloud fallback。

## Phase 3：地端一鍵安裝器

### 3.1 Artifact manifest

新增：

- `src/installer/artifacts.py`
- `tests/test_artifact_manifest.py`

固定 Qwen GGUF revision／URL／bytes／SHA-256，以及 llama.cpp `b10625` macOS ARM64 archive／commit／bytes／SHA-256。URL 僅允許固定 HTTPS host 與 immutable path。

### 3.2 可續傳 downloader

新增：

- `src/installer/downloader.py`
- `tests/test_downloader.py`

功能：

- 以 argument array 呼叫系統 `curl`，`shell=False`。
- `.part`＋HTTP Range 續傳、retry、timeout、HTTPS-only、redirect 後 scheme／host 驗證。
- server 忽略或拒絕 range 時安全重新下載。
- bytes／SHA-256 驗證後 atomic rename；錯誤正式檔 quarantine。
- 本機 HTTP fixture 覆蓋完成、中斷、續傳、404、timeout、redirect、tamper 與 range anomaly。

### 3.3 安全 archive extraction

新增：

- `src/installer/safe_archive.py`
- `tests/test_safe_archive.py`

功能：

- hash 通過後才解析。
- 拒絕 absolute path、`..`、symlink、hardlink、device、FIFO、重複 member 與未知頂層目錄。
- 解壓至同 filesystem staging；固定 inventory、Mach-O arm64 與 `llama-server --version` 驗證後 atomic promote。
- manifest 記錄相對路徑、bytes、SHA-256、mode 與 runtime identity。

### 3.4 強化 installer

修改：

- `src/installer/qwen_installer.py`
- `tests/test_qwen_installer_lifecycle.py`

內容：

- 加入 default managed root、owner／symlink／specificity／Production path boundary。
- 整合 artifact cache、download、secure extraction、transaction state、stale staging cleanup 與 idempotent install。
- manifest schema 升版並綁定 archive、runtime inventory、model identity、platform 與 install root。
- uninstaller 只移除 manifest allowlist；unknown file、symlink、identity mismatch 一律 fail closed。

### 3.5 生命週期與單一 CLI

修改／新增：

- `src/lifecycle/llama_server_manager.py`
- `scripts/qwen_local.py`
- `qwen-local`
- `tests/test_qwen_local_cli.py`
- `tests/test_lifecycle.py`

指令：`install／verify／start／stop／status／health／uninstall`。

要求：

- status 為 redacted JSON；不輸出 credential、全文、向量或敏感絕對路徑。
- start 冪等；pid metadata 綁定 executable、model、port 與 start time，不能誤殺其他程序。
- sidecar 固定 loopback、embedding-only、pooling last、Web UI 關閉、parallel 1。
- canary 驗證 2,560 原生維度、finite、non-zero norm；provider 對外取前 768 維後重新 L2 normalize。

## Phase 4：地端 repository 驗證與發布

### 4.1 本機測試

- Python unit／integration 全測試。
- template Node tests、postrun、snapshot、archive parity、dangerous-exec。
- downloader fixture、secure tar、target／uninstall、provider isolation、offline、port collision、stale PID。
- 使用官方 llama.cpp `b10625` 做真實 install／start／health／embedding／stop／uninstall 兩輪 rehearsal。
- 重用既有已驗證 Qwen GGUF，避免重複 2.69 GiB 下載；另用 fixture 測完整 downloader 行為。
- 重跑固定 20 題 benchmark：Hit@5 ≥ 85%、MRR ≥ 0.7167、p95 ≤ 1 秒。
- 與原 runtime 做 deterministic vector parity；任何品質 Gate 退步即 BLOCK。

### 4.2 安全與供應鏈

- OWASP A01–A10 evidence register 全部有結論。
- dependency audit、license／artifact inventory、secret scan、large-file scan、cloud-runtime negative scan。
- 攻擊者視角 review：path injection、manifest forgery、archive traversal、PID reuse、download tamper、disk exhaustion、partial install、cleanup failure。

### 4.3 GitHub 發布

- push feature branch，建立 PR，等待 CI success。
- 獨立 reviewer review exact commit、diff、tests、OWASP evidence 與 archive。
- P0／P1=0 且 reviewer PASS 才 merge `main`。
- 設定 repository description、topics、README cross-link；不建立 production release tag，除非另有 Human Gate。

## Phase 5：Gemini repository 清理

### 5.1 建立 clean branch

- 回到既有 repository `main` exact remote HEAD；確認 clean。
- 建立 `feat/gemini-only-repository-20260901`。

### 5.2 移除 Qwen 產品碼

刪除：

- `src/installer/`
- `src/lifecycle/`
- Qwen shadow／day3／day5／progress scripts。
- Qwen installer／shadow／progress tests。
- template `src/embed-qwen.js`、`src/shadow-index.js` 與 Qwen product tests／commands。
- Qwen customer installer spec／plan若會讓使用者誤判此 repo 提供地端產品；拆分設計保留作 provenance。

修改：

- template `src/cli.js` 移除 Qwen import／provider branch。
- bootstrap 固定 Gemini `embedding-001` 產品路徑；保留外送 approval gate。
- README／SKILL／architecture 首屏標示 Gemini cloud embedding；連結地端 repo。
- `.github/workflows/ci.yml`、archive builder／tests 與 distribution archive。

local-hash 若只用於 deterministic tests，移至 test fixture；production provider 僅允許 `google-gemini`。

### 5.3 Gemini regression Gate

- Gemini bootstrap、privacy approval、cache、768 維 index/query、incremental、audit、snapshot、archive parity 全測試。
- Qwen production symbol negative scan。
- OWASP A01–A10、dependency、secret、dangerous-exec、README link、GitHub CI。
- 獨立 reviewer review exact commit；P0／P1=0 才 merge `main`。

## Phase 6：雙 repo closeout

- 回讀兩個 GitHub repo visibility、default branch、description、README cross-links、PR、CI、merge commit。
- clone 兩個 `main` 至 fresh temp directories，執行 provider boundary scan 與最小 smoke。
- 驗證 skill ids、archive names、default provider、table/cache/state identity 不互相碰撞。
- 確認 Production Gemini config／DB／cache／schedule hash 與前測一致，沒有 cutover 或資料 mutation。
- 最終回報：repo URLs、changed files 摘要、tests/logs、OWASP evidence、PR、CI、merge commits、remaining blockers、dirty status。

## 停止條件

- 新 repo 名稱或 visibility 與規格不符。
- 來源或目標 worktree 有未知 dirty changes。
- 官方 artifact identity、checksum、license 或 runtime parity 不一致。
- 地端 repo 仍有 Gemini runtime／cloud fallback，或 Gemini repo 仍有 Qwen 產品入口。
- installer target 可能碰到 Production、home root、workspace root 或非受管資料。
- P0／P1、secret、模型／corpus／vector 入 Git、CI failure 或 reviewer 非 PASS。
- 任一必要變更需要降低已簽收的品質、安全、平台或 rollback Gate；此時先回報 Jasper，不自行降規。
