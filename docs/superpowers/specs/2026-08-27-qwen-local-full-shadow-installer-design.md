# Qwen 本機 Embedding 一鍵安裝與全量 Shadow 驗證設計

日期：2026-08-27

狀態：`APPROVED_IN_PROGRESS`

核准背景：Jasper 已在 Discord 核准執行五日隔離測試；本文件把已確認方向固化成可追溯規格。

## 一句話目標

在不修改現行 Gemini Production 的前提下，完成 macOS Apple Silicon 的 Qwen3-Embedding-4B Q5 一鍵安裝測試版、全量獨立索引與五日自動驗證，最後交付是否可正式取代 Gemini 的證據與白話報告。

## 現況基線

- 前一輪 1,000 chunks POC 已通過：Qwen Q5 768 維與 Gemini 同為 Hit@5 95%、MRR 0.900。
- 2026-08-27 唯讀掃描為 4,653 份文件、94,800 個候選 chunks。
- 現行 Gemini 表為 94,700 rows；舊 manifest 內的 chunks 統計與資料表 row count 不一致。本輪不得沿用單一舊數字作全量完成證據，必須凍結新的來源快照並做逐層對帳。
- Q5 官方 GGUF、Q4 備援 GGUF與固定 revision 的 `llama-server` 已存在本機 POC 隔離目錄，且先前已驗證 SHA-256。
- 工作磁碟目前有足夠空間；正式 preflight 仍必須重新計算模型、索引、checkpoint、log 與安全餘量。

## 範圍

### 本輪包含

- macOS Apple Silicon 第一版一鍵安裝／設定／健康檢查／解除安裝測試流程。
- `qwen-local` embedding provider，固定 Q5_K_M、768 維、last-token pooling、截斷後重新 L2 normalization。
- 僅綁定 `127.0.0.1` 的 `llama-server` lifecycle manager。
- 獨立 Qwen shadow config、embedding cache、checkpoint、LanceDB 路徑與 table identity。
- 可中斷續跑的全量索引與來源／chunk／row／fingerprint 對帳。
- 20 題人工 ground-truth benchmark、端到端延遲、記憶體、重啟、崩潰、增量、離線、磁碟不足與清理測試。
- 五日自動 checkpoint、失敗續跑、每日狀態與最終完整報告。

### 本輪不包含

- 不修改、覆寫或刪除現行 Gemini DB、cache、config、排程或 Production 搜尋。
- 不執行 Production provider 切換，不移除 Gemini fallback 資產。
- 不把模型、內部 corpus、向量、秘密或完整敏感 log 提交 GitHub。
- 不承諾 Windows、Linux、Intel Mac 或客戶異質硬體已通過；它們屬後續相容性矩陣。
- 不加入靜默 cloud fallback；本機 Qwen 不可用時必須 fail closed。

## 方案比較與選定

### A. 一鍵安裝器＋正式 provider 抽象＋全量 shadow（採用）

一次驗證未來產品真正需要的安裝、索引、查詢、續跑與清理路徑。工程量較高，但避免做出只能跑 benchmark、不能交付客戶的第二套腳本。

### B. 只用既有 POC 腳本建立全量向量

最快取得全量品質數字，但無法證明安裝器、provider 切換、生命周期、增量索引與故障恢復，因此不能作為產品放行證據。

### C. Qwen／Gemini 自動 fallback 雙 provider

短期可提高可用性，但會保留外送、API 設定與雙索引維運成本，也可能在本機失敗時違反客戶「資料不外傳」期待。本輪不採用。

## 架構設計

### 1. 安裝器與 preflight

- 偵測 OS、CPU architecture、RAM、可用磁碟與必要命令。
- 第一版只允許已驗證的 macOS Apple Silicon 路徑；不支援的平台清楚停止，不嘗試猜測安裝。
- 固定 Qwen GGUF revision、檔名、SHA-256、llama.cpp revision 與授權 notices。
- 已有且 hash 正確的 artifact 可重用；hash 不符時隔離並重新取得，不能直接執行。
- 模型不可進 Git；下載支援暫存檔、續傳、原子 rename 與 checksum readback。
- 產生本機隨機 API credential，只存權限受限的本機設定檔；log、錯誤與報告不得輸出值。

### 2. Qwen provider

- provider id：`qwen-local`。
- 只接受 loopback HTTP(S)；拒絕 LAN、公網 hostname 與自動 cloud fallback。
- 文件原文直接 embedding；查詢固定使用官方 retrieval instruction。
- 驗證回傳筆數、順序、原生維度、有限數值與 norm；取前 768 維後重新 L2 normalize。
- embedding identity 必須包含 provider、模型 revision、quantization、GGUF hash、llama.cpp revision、pooling、query instruction、dimensions 與 normalization contract。
- identity 任一欄位改變都必須建立新 shadow index，不得增量混用。

### 3. Sidecar lifecycle manager

- 啟動參數固定為 embedding-only、pooling last、Web UI 關閉、loopback only、受限 batch／ubatch／context 與單機低並發。
- 啟動後先做 `/health` 與已知 embedding canary；未通過不得開始索引。
- 正常關閉採單次 `SIGTERM` 並等待；逾時後才升級處理。
- stale pid、port collision、程序崩潰與非預期退出都必須可辨識；重啟不得產生第二個 sidecar。
- lifecycle failure 不得把不完整索引標成完成。

### 4. 全量 shadow index

- 從現行 source map 產生唯讀來源快照；Production config 本身不改寫。
- 使用獨立目錄、table、state、cache、checkpoint 與 run id。
- 索引前保存來源清單、每檔 hash、eligible chunks、排除原因與 embedding fingerprint。
- 分批寫入後保存 durable checkpoint；中斷後從已驗證批次續跑，不重新寫入已完成 chunk。
- 完成時驗證：來源快照 → eligible chunks → embedding cache → LanceDB rows → index state 完全一致，並檢查 chunk id 唯一、維度 768、全部有限數值、fingerprint 單一。
- 任何不一致都標 `BLOCKED_RECONCILIATION`，不得用大約數字放行。

### 5. 五日自動驗證

- Day 1：安裝器、provider、lifecycle 與 fresh isolated smoke；啟動全量 shadow build。
- Day 2：完成或續跑全量索引；做 row／fingerprint／來源對帳與 20 題 benchmark。
- Day 3：重啟、SIGTERM、強制中斷、stale pid、port collision、斷網查詢與增量 fixture 測試。
- Day 4：週期性 Qwen／Gemini read-only benchmark、查詢延遲、記憶體與長時間穩定性測試。
- Day 5：fresh reinstall／uninstall／restore rehearsal、攻擊者視角 review、完整測試、Git closeout 與白話報告。
- 每階段寫 checkpoint；Gateway 或 session 中斷後可由排程讀取 checkpoint 續跑。

### 6. 報告與資料最小化

- Git 只保存程式、測試、規格、去識別彙總 JSON 與報告。
- corpus 原文、向量、API credential、完整本機路徑與模型檔不進 Git／Notion／Discord。
- 最終報告包含：品質、速度、記憶體、索引時間、故障測試、安裝體驗、安全 Gate、限制、是否建議切換與下一個 Human Gate。

## 驗收標準

- Fresh isolated install 不需手動改程式或貼 API key，安裝後本機 canary PASS。
- 安裝完成後斷網仍可索引既有本機資料與查詢；不得發生 cloud fallback。
- 全量來源、eligible chunks、cache、rows、state 與 fingerprint 對帳一致，重複 chunk id 為 0。
- Qwen Hit@5 不低於 Gemini 超過 5 個百分點；MRR 不低於 Gemini 超過 0.05。
- 20 題全量端到端查詢 p95 目標不高於 1 秒；若超過則標示性能 blocker，不以 POC embedding-only latency 代替。
- 全量首次重建在本機目標 20 小時內完成；中斷續跑不得重建已完成批次。
- 峰值 memory footprint 目標不超過 13.5GiB；若造成明顯 memory pressure／swap 失控則 BLOCK。
- 正常停止、強制終止與重啟測試均不得污染索引或遺留多重 sidecar。
- 增量 fixture 的新增、修改、刪除、重跑與重複事件保持 exactly-once 結果。
- 解除安裝只移除 Qwen 受管 artifact；不得碰 Gemini、來源文件或其他 OpenClaw 資料。
- 完整 unit／integration／postrun／dependency／secret scan PASS；P0／P1 為 0。

## SECURITY_SCOPE

- `data_classification`：內部知識文件 D1／D2；credential、token、private key 永不索引。
- `trust_boundaries`：官方模型與 llama.cpp 供應鏈、本機安裝器、loopback sidecar、shadow runner、來源文件、獨立 LanceDB。
- `roles_and_tenants`：本輪單機單管理者、無新登入；仍需防來源跨 project 誤混與報告洩漏。
- `external_services_and_costs`：首次下載官方 artifact；Gemini 只執行既有核准的 read-only benchmark queries，不重新外送全文。
- `ai_tools_and_write_capabilities`：Qwen 只產生 embeddings，無工具權限、無外部寫入；測試 runner 只可寫隔離 shadow 路徑。

## THREAT_MODEL

- 供應鏈 artifact 被替換：immutable revision＋SHA-256＋授權與來源驗證。
- sidecar 被 LAN／公網存取：loopback allowlist＋啟動參數回讀＋negative connection tests。
- 索引身份混用：完整 fingerprint＋獨立 table／path＋完成前 reconciliation。
- 惡意文件注入指令或 shell 字串：內容只作 embedding input，不解析、不執行、不串接 shell。
- 資源耗盡：磁碟 preflight、batch／concurrency／context 上限、checkpoint、取消與 fail closed。
- 不完整工作被誤報成功：terminal state、checkpoint schema、row reconciliation 與明確 BLOCK 狀態。
- 秘密進 log／報告／Git：redaction、禁止 corpus／vector commit、secret scan 與人工 diff review。

## OWASP_2025_PLAN

- A01：驗證 runner 只可寫 shadow roots，Production 路徑與 Gemini 資產 negative test。
- A02：驗證 loopback-only、Web UI 關閉、無 cloud fallback、設定與環境隔離。
- A03：模型／runtime pin、hash、license、lockfile、dependency audit 與 artifact inventory。
- A04：本機 API credential 使用安全隨機、權限受限、不可入 log／Git；不自製加密。
- A05：路徑、URL、模型回應與 CLI 參數做 allowlist／schema 驗證；惡意檔名與 shell payload 測試。
- A06：資源上限、idempotency、resume、Production 不可變邊界與無自動切換。
- A07：`NOT_APPLICABLE_WITH_EVIDENCE`；本輪無登入／session，但本機 credential lifecycle 仍按 A04／A09 驗證。
- A08：artifact hash、embedding fingerprint、checkpoint schema、row integrity 與 corruption negative tests。
- A09：保留 run id、階段、進度、失敗與資源摘要；不得記錄秘密、全文或向量。
- A10：下載中斷、sidecar crash、timeout、port collision、磁碟不足、重複執行、部分成功、resume 與 cleanup fault tests。
- `BUSINESS_LOGIC_ABUSE_CASES`：重複安裝、重複啟動、重複 checkpoint、錯誤 fingerprint 續跑、Production 路徑注入、超大文件／batch、解除安裝指錯路徑。
- `AI_SECURITY_OVERLAY`：required；不可信文件只送 embedding，模型無工具，輸出以 deterministic schema 驗證。
- `ASVS_LEVEL_TARGET`：`not_applicable_with_reason`；本輪無公開 Web／產品 API，採本機 CLI／供應鏈／程序／資料完整性等價控制。
- `HUMAN_SECURITY_GATES`：本輪只放行隔離測試。Production provider 切換、刪除 Gemini、客戶正式發佈與跨平台 installer 另需 Jasper 核准。

## 回滾與停止條件

- 任一命令發現 target 落入現行 Gemini DB、cache、config 或排程時立即停止。
- RAM、swap、thermal 或磁碟達安全門檻時暫停並保存 checkpoint，不硬跑。
- fingerprint、rows 或來源對帳失敗時保留證據並 BLOCK，不刪除 Production 或原始資料。
- installer／uninstaller 只能操作具受管 manifest 且位於 allowlisted root 的 artifact；target 不明時拒絕動作。
- 本輪無 Production cutover，所以回滾是停止 sidecar、保留或移除 shadow 受管資產；Gemini 維持原狀。

## 書面簽收後的下一步

1. 建立詳細 implementation plan 與檔案／測試對應。
2. 先寫失敗測試與隔離保護，再實作 provider、lifecycle、installer、resume runner。
3. 完整本機驗證後才啟動全量 shadow build 與五日排程。
4. 五日結果通過也只產生「可申請切換」建議；Production 仍需獨立 Human Gate。
