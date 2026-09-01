# Gemini 與地端 Embedding 雙 Repository 拆分設計

日期：2026-09-01

狀態：`WRITTEN_SPEC_PENDING_USER_REVIEW`

## 一句話目標

把目前同時含 Gemini 與 Qwen 候選功能的公開 repository，拆成一個只面向 `gemini-embedding-001` 的既有 repository，以及一個只面向 Qwen 本機 embedding 的新公開 repository；保留可追溯 Git 歷史，不改寫既有公開歷史，也不自動切換 Production。

## 已確認決策

- 拆分方式採「保留 Git 歷史」方案。
- 既有公開 repository 保持名稱 `JasperYang0609/openclaw-lancedb-knowledge-skill`。
- 新地端公開 repository 名稱為 `JasperYang0609/openclaw-lancedb-knowledge-embedding-local`。
- 兩個 repository 都公開。
- Gemini repository 的產品預設固定為 `gemini-embedding-001`。
- 地端 repository 的產品預設固定為 `Qwen3-Embedding-4B-Q5_K_M`、768 維。
- 地端第一版正式支援 macOS Apple Silicon。
- Production Gemini、現有索引、cache、config、排程與使用者資料不在本次變更範圍。

## 為什麼採保留歷史拆分

新地端 repository 從目前已包含五日 Qwen shadow 驗證的 lineage 建立，因此 installer、benchmark、安全修復與驗證證據可追溯。既有 Gemini repository 以新的 forward-only commit 移除 Qwen 產品碼，不 force-push、不 rebase 公開 `main`，避免破壞既有 clone、commit 連結與安裝來源。

不採乾淨重建，因為會失去 Qwen 測試與修復的來源鏈。不採第三個共享核心 repository，因為目前功能規模不足以抵銷三個 repository、submodule 或套件發布帶來的維護與供應鏈成本。

## Repository 邊界

### 1. Gemini repository

Repository：`JasperYang0609/openclaw-lancedb-knowledge-skill`

產品責任：

- 使用 Google Gemini `gemini-embedding-001` 建立與查詢 768 維向量。
- 保持既有 repository URL、skill 名稱與安裝方式相容。
- 保留 Gemini 所需 provider、cache、設定、bootstrap、測試與安全檢查。
- README 首段明確標示「Gemini 雲端 Embedding 版」，說明內容會送往 Google API，並要求既有 approval gate。
- README 連結至地端 repository，供不允許資料外送或希望免 API 用量者選擇。

必須移除：

- Qwen runtime provider、sidecar lifecycle、installer、shadow runner、Qwen customer CLI。
- Qwen 專用 scripts、測試、CI 工作、產品設定範例與 distribution payload。
- 把 Qwen 描述為此 repository 可安裝產品功能的 README／SKILL 文件內容。

可保留：

- 公開 Git 歷史中的既有 Qwen commits，不改寫歷史。
- 必要的拆分公告與新 repository 連結。
- 與 Gemini 本身直接相關、且不會讓使用者誤以為本 repository 支援 Qwen 的一般性研究紀錄。

### 2. 地端 repository

Repository：`JasperYang0609/openclaw-lancedb-knowledge-embedding-local`

產品責任：

- 使用 Qwen3-Embedding-4B Q5_K_M、768 維建立與查詢本機向量。
- 文件與查詢只送往 `127.0.0.1` 的受管 llama.cpp sidecar。
- 不要求 Gemini API key，不含 runtime cloud fallback，不將 corpus 或向量送往外部服務。
- 提供單一 customer-facing CLI：`qwen-local install|start|stop|status|health|uninstall`。
- 自動取得固定版本的官方 Qwen GGUF 與 llama.cpp macOS ARM64 release，支援中斷續傳、SHA-256 驗證、原子安裝、安全解壓與冪等重跑。
- README 首段明確標示「macOS Apple Silicon 地端版」、硬體需求、下載容量、首次建索引時間與不支援平台。
- README 連結至 Gemini repository，供低本機資源或偏好雲端者選擇。

必須移除或停用：

- Gemini runtime provider、Gemini API key 讀取、Google endpoint、cloud fallback 與 Gemini 產品 bootstrap 選項。
- 會把文件或查詢送往外部 embedding API 的產品路徑。
- 任何將模型、向量、corpus、credential、完整本機 log 或暫存資料提交 Git 的可能性。

可保留：

- 五日 shadow 驗證、Qwen/Gemini 對照與選型報告，作為地端模型的公開決策證據；必須標示它們是歷史 benchmark，而非 runtime Gemini 相依。
- 與公開模型、llama.cpp revision、license、checksum、安全 Gate 直接相關的規格與報告。

## 拆分與發布流程

1. 在既有 repository 建立專用拆分 branch，確認 `main`、HEAD、remote 與 worktree clean。
2. 以目前含 Qwen 驗證 lineage 的 exact commit 建立新本機 clone，再建立新的 GitHub 公開 repository 與 `origin`；不共享可變的 working tree。
3. 先在地端 repository 移除 Gemini runtime，完成 customer CLI、自動下載／續傳與安全 Gate；所有變更經 branch、commit、PR、CI 後才合併 `main`。
4. 再在 Gemini repository 以 forward-only branch 移除 Qwen 產品碼，更新 README／SKILL／distribution；所有變更經 branch、commit、PR、CI 後才合併 `main`。
5. 兩個 repository 的 README、description、topics 與 release notes 互相連結，清楚說明選型與資料流。
6. 最終確認兩邊 `main` 的 runtime 依賴、產品文件、distribution archive 與 CI 都只包含各自 provider。

不得使用 force-push、歷史重寫或刪除既有公開 repository。建立新 GitHub repository、推 branch、PR 與 merge 屬本規格核准後的正常發布步驟。

## 地端單一 CLI 設計

### 指令

- `qwen-local install`：preflight、下載、續傳、checksum、解壓、安裝、啟動與 canary。
- `qwen-local start`：冪等啟動唯一 sidecar；已有健康程序時直接成功。
- `qwen-local stop`：只停止 manifest 所屬程序，不以模糊 PID 或名稱殺程序。
- `qwen-local status`：回報安裝狀態、版本、健康、受管檔案與索引相容性摘要，不輸出秘密。
- `qwen-local health`：執行 loopback health 與已知 embedding canary。
- `qwen-local uninstall`：只移除 manifest 列出的受管 artifact；不碰來源文件、索引、Gemini 資產或其他 OpenClaw 資料。

### 下載與安裝

- 使用 macOS 系統 `curl`，以 `.part` 檔與 HTTP range 續傳；server 不支援 range 時安全重新下載。
- artifact URL、release revision、檔名、大小與 SHA-256 固定在受版本控制的 manifest。
- checksum 不符時 fail closed，保留可診斷的非敏感摘要，不執行 artifact。
- archive extraction 先檢查 absolute path、`..` traversal、symlink、hardlink 與重複路徑，再解壓到 staging root；驗證後以原子 rename 安裝。
- runtime 只綁 `127.0.0.1`，Web UI 關閉，使用權限受限的本機隨機 credential。
- installer target 必須通過 allowlist、owner、symlink 與 specificity 檢查；不允許 workspace root、home root、filesystem root 或 Gemini production path。

## 資料與遷移行為

- 兩個 repository 使用不同 provider identity、table name、cache、state 與 managed root。
- 安裝地端版不讀寫 Gemini table、cache、config 或排程。
- 不自動搬移或重用 Gemini vectors；因 embedding identity 不同，地端版必須建立自己的索引。
- 本次不執行 Production cutover。未來若要切換，需另立 Human Gate、最新 corpus reconciliation、canary、觀察窗與 rollback plan。

## 錯誤處理與回滾

- 任一下載、checksum、解壓、health、fingerprint 或安全檢查失敗即停止，不把 partial install 標為成功。
- 中斷後可從已驗證的 `.part` 或 checkpoint 續跑；重跑保持冪等。
- GitHub 建 repo 後若地端 Gate 未通過，不合併 `main`，repository 保持未發布狀態並清楚標示 blocked。
- Gemini repo 的 Qwen 移除只有在地端 repo 已可安裝、文件可讀且 CI 通過後才合併，避免出現沒有可用替代入口的空窗。
- Production 沒有變更，因此回滾不涉及服務切換；Git 回滾採新增 revert commit，不重寫歷史。

## 驗收標準

### Gemini repository

- `main` runtime、README、SKILL、bootstrap、tests、distribution archive 不提供 Qwen 產品功能。
- Gemini `gemini-embedding-001` 既有 bootstrap、index、query、cache 與 approval tests 全部通過。
- 既有 repository URL 與安裝入口不變。
- README 可在首屏判斷這是雲端 Gemini 版並找到地端替代連結。

### 地端 repository

- 公開 repository 名稱與 visibility 正確，default branch 為 `main`。
- Fresh macOS Apple Silicon 環境可用單一 CLI 完成安裝，無須貼 Gemini API key或手動編譯。
- 下載中斷後可續傳；checksum 錯誤、惡意 archive、非支援平台、port collision、stale PID、磁碟不足與重複執行皆 fail closed 或安全恢復。
- 安裝、start、status、health、stop、uninstall 皆有 unit／integration tests；至少兩輪 fresh install／uninstall／restore rehearsal 通過。
- 斷外網後，已安裝環境仍可對既有本機資料執行 embedding 與查詢；無 cloud fallback。
- Qwen 20 題 benchmark 保持 Hit@5 不低於 85%、MRR 不低於 0.7167、端到端 p95 不高於 1 秒。
- release artifact、source tree 與 distribution archive parity 通過；模型、corpus、向量、秘密與暫存資料不在 Git。

### 共同發布 Gate

- 兩邊 fresh tests、dependency audit、secret scan、dangerous-exec scan、license／artifact inventory、README link check 與 GitHub CI 全部通過。
- 兩邊 `main` worktree clean、local／remote HEAD 一致，並提供 commit hash、PR、CI 與 remaining blocker。
- 任一 P0／P1、未處理安全項或沒有可重現證據時不得宣稱完成。

## SECURITY_SCOPE

- `data_classification`：內部文件 D1／D2；token、API key、private key、個資與敏感 corpus 不進 Git、報告或公開 log。
- `trust_boundaries`：GitHub 公開 source、官方 Qwen／llama.cpp artifact 供應鏈、本機 installer、loopback sidecar、來源文件、各自獨立的 LanceDB／cache／state。
- `roles_and_tenants`：公開 repository 維護者與一般安裝者；runtime 為單機單管理者，不新增登入系統。
- `external_services_and_costs`：Gemini repo 會依使用者明確核准呼叫 Google API；地端 repo 只在安裝時下載公開 artifact，runtime 不呼叫 cloud embedding。
- `ai_tools_and_write_capabilities`：embedding 模型無工具權限；CLI 只可寫明確受管 root 與獨立索引路徑。

## THREAT_MODEL

- 使用者裝錯版本：名稱、首屏說明、互相連結與 provider identity fail closed。
- 拆分後仍殘留交叉 provider：runtime import scan、config scan、archive parity 與 negative tests。
- 供應鏈 artifact 被替換：immutable revision、SHA-256、來源 allowlist、license inventory。
- 路徑穿越或惡意 archive：staging、entry validation、symlink／hardlink 拒絕與原子安裝。
- sidecar 被 LAN 或公網存取：loopback-only、Web UI 關閉、negative listener test、本機 credential。
- 不完整安裝誤報成功：transaction state、manifest、health canary、terminal status 與冪等 resume。
- uninstaller 誤刪資料：manifest identity、hash、allowlisted root、unknown-file 與 symlink fail closed。
- 秘密或 corpus 進 Git：ignore rules、secret scan、large-file scan、人工 diff review。

## OWASP TOP 10:2025 驗證計畫

- A01 Broken Access Control：驗證 CLI 只能寫受管 root；Gemini／地端跨路徑與 Production path negative tests。
- A02 Security Misconfiguration：驗證地端 loopback-only、Web UI 關閉、無 cloud fallback；兩 repo provider 預設與文件一致。
- A03 Software Supply Chain Failures：固定 artifact／dependency revision、checksum、license、SBOM 或等價 inventory、dependency audit。
- A04 Cryptographic Failures：本機 credential 使用 CSPRNG、檔案權限受限；不用自製加密；credential 不進 log／Git。
- A05 Injection：CLI 參數、URL、archive entry、路徑與模型回應 schema 驗證；shell payload、惡意檔名與 traversal tests。
- A06 Insecure Design：獨立 provider identity、fail closed、冪等、resume、無自動 Production cutover。
- A07 Authentication Failures：`NOT_APPLICABLE_WITH_EVIDENCE`；無遠端登入或 session。地端 credential lifecycle 由 A02／A04 驗證。
- A08 Software or Data Integrity Failures：checksum、manifest identity、embedding fingerprint、archive parity、row／state integrity。
- A09 Security Logging and Alerting Failures：記錄非敏感 run id、phase、status 與錯誤分類；禁止全文、向量與秘密。
- A10 Mishandling of Exceptional Conditions：下載中斷、range 不支援、磁碟不足、port collision、stale PID、部分解壓、重複執行、cleanup failure 與 rollback tests。
- `BUSINESS_LOGIC_ABUSE_CASES`：安裝錯 repo、重複安裝／啟動／解除安裝、provider fingerprint 混用、Production path 注入、偽造 manifest、超大下載與磁碟耗盡。
- `AI_SECURITY_OVERLAY`：required；文件僅作 embedding input，不解析為指令、不執行工具；模型輸出以 deterministic schema 驗證。
- `ASVS_LEVEL_TARGET`：`not_applicable_with_reason`；沒有公開 Web／產品 API 或登入面。以 CLI、供應鏈、程序、檔案權限與資料完整性測試提供等價證據。

## 明確不做

- 不 force-push、不重寫或刪除公開 Git 歷史。
- 不在本次建立第三個 shared-core repository。
- 不支援 Windows、Linux 或 Intel Mac 第一版 installer。
- 不自動切換 Production、不刪除 Gemini 資產、不重用 Gemini vectors。
- 不把模型檔打包進 GitHub repository 或 skill archive。
- 不因拆分而降低既有五日 Qwen shadow 品質、安全或故障恢復 Gate。

## 書面簽收後的下一步

1. 建立逐檔 implementation plan 與兩個 repository 的 task spec／驗證矩陣。
2. 先建立地端 repository 與隔離 branch，完成純地端 runtime、單一 CLI、自動下載／續傳與 release Gate。
3. 地端版通過後，再以 forward-only PR 清理 Gemini repository 的 Qwen 產品碼。
4. 兩邊 CI、README、archive 與 cross-link 完成後合併，交付兩個 repository URL、PR、commit、測試證據與 remaining blockers。
