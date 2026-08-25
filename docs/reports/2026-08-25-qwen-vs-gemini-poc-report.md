# Qwen3-Embedding-4B 與 Gemini Embedding 實機對照報告

日期：2026-08-25
機器：Apple M1 Pro、16GB 統一記憶體
結論狀態：POC 通過；Production 切換尚未放行

## 白話結論

**本機 Qwen 可以取代 Gemini，而且這輪搜尋品質沒有明顯退步。**

最公平的 768 維純向量測試中：

- Gemini：20 題答對 19 題，命中率 95%，答案排名分數 0.90。
- Qwen Q5：20 題答對 19 題，命中率 95%，答案排名分數也是 0.90。
- Qwen Q4：同樣答對 19 題，但答案排名分數是 0.867，稍弱於 Gemini／Q5。

因此正式方向建議採 **Qwen Q5_K_M＋768 維**。它與 Gemini 同分，模型檔約 2.69GiB；Q4 約少 390MB、快約 9%，但答案排名略差，較適合低資源備援。

把 Qwen 開到原生 2,560 維沒有變準，反而略降，索引也會膨脹約 3.3 倍。因此沒有理由為本案採 2,560 維。

## 測試回答了什麼

本輪不是用公開排行榜猜結果，而是使用安賽現有資料與問題實測。公開 Qwen 榜單比較的 Gemini 版本並不是本案正在用的 `gemini-embedding-001`，不能直接當作替代證據。

主測固定：

- 同一批 1,000 段內容。
- 同一組 20 題人工基準。
- 同樣取前五名。
- Gemini 與 Qwen 都使用 768 維並做相同正規化。
- 不加關鍵字或日期加分，以純向量搜尋隔離模型差異。

1,000 段樣本含 239 段指定答案來源與 761 段固定干擾內容；抽樣 seed 為 `qwen-gemini-poc-v1`。現行正式表共有 93,525 rows。Gemini 的 20 句查詢因舊快取不完整而重新取得，但文件內容沒有重新送往 Gemini；Qwen 的文件與查詢全部在本機處理。

## 品質結果

| 模型 | 維度 | Hit@5 | MRR | 判讀 |
|---|---:|---:|---:|---|
| Gemini embedding-001 | 768 | 95% | 0.900 | 現行基準 |
| Qwen Q5_K_M | 768 | 95% | 0.900 | 與 Gemini 同分，建議預設 |
| Qwen Q5_K_M | 2,560 | 95% | 0.892 | 沒有變準，不建議 |
| Qwen Q4_K_M | 768 | 95% | 0.867 | 稍弱，低資源備援 |
| Qwen Q4_K_M | 2,560 | 95% | 0.867 | 沒有變準，不建議 |

Q5 與 Gemini 都只漏掉同一題。兩者的差別主要是兩題的第一、第二名互換，總 MRR 剛好相同。這比只看「答對幾題」更可靠，因為它同時檢查答案是否排得夠前面。

既有關鍵字／日期混合排序另做次要測試：Gemini 為 85%／0.788；Qwen Q5 768 為 90%／0.850。這表示 Qwen 放回現有產品排序邏輯後沒有退步，但主結論仍以純向量結果為準，避免排序規則遮住模型差異。

現行 93,525 rows 全量索引在 2026-07-14 的舊基準是 90%／0.625；它與本輪 1,000 段 shadow corpus 的範圍不同，不能直接橫向比較。

## 速度與電腦負擔

| 項目 | Q5_K_M | Q4_K_M |
|---|---:|---:|
| 模型檔 | 2.69GiB | 2.33GiB |
| 建立 1,000 段 | 9 分 39 秒 | 8 分 50 秒 |
| 平均速度 | 1.73 段／秒 | 1.89 段／秒 |
| 單題本機 embedding p50 | 0.115 秒 | 0.106 秒 |
| 單題本機 embedding p95 | 0.120 秒 | 0.112 秒 |
| macOS peak memory footprint | 約 10.9GiB | 約 10.6GiB |

以本輪速度線性估算，93,525 rows 首次全量重建約需 14–15 小時，所以應設計為安裝後的背景／夜間工作。之後只更新少量新文件時，不會每次重跑全部資料。

16GB 這台電腦能跑，但峰值 footprint 約 11GiB，不能把它當成「完全沒負擔」。正式包裝應採需要時啟動、限制並發、依 token 數量分批，並避免與其他大型模型同時運行。

這裡的約 0.11 秒是產生查詢向量的時間，尚未包含完整 93,525 rows LanceDB 搜尋與產品 rerank；全量查詢延遲仍需下一階段 shadow index 實測。

## 正確性與穩定性

- 兩輪各產出 1,000 個 2,560 維向量，全部為有限數值，正規化後長度約為 1。
- 長段落有觸發 llama.cpp 自動縮小批次與 prompt cache 清理，但兩輪都是零截斷、零失敗。
- Metal 重複查詢有極小浮點差異，最大絕對差約 `1.15e-4`；驗收需用容差，不可要求 bit-for-bit 完全相同。
- Q5 測試服務以互動式雙重 Ctrl-C 結束時觸發 Metal cleanup assertion；改以單次 `SIGTERM` 關閉 Q4 時正常退出。正式 lifecycle manager 必須採 `SIGTERM`、等待退出並測 crash recovery，不能沿用互動式中斷方式。
- Q5、Q4 都使用官方 GGUF 並通過 SHA-256；llama.cpp 從固定 revision 編譯，服務只綁定 `127.0.0.1`、停用 Web UI 並要求本機 API key。

## 建議產品設定

- 預設模型：`Qwen3-Embedding-4B-Q5_K_M.gguf`。
- 預設向量：768 維。
- 查詢：固定 Qwen 官方建議的英文 retrieval instruction。
- 文件：不加 query instruction。
- pooling：last token；輸出做 L2 normalization。
- 服務：localhost only、無 cloud fallback、需要時才啟動。
- 初次建索引：背景／夜間工作、可暫停續跑、有明確進度與剩餘時間。
- Q4：只在客戶磁碟／記憶體條件較差，且接受排名略降時選用。

## 不能直接切 Production 的原因

這輪 POC 已證明方向可行，但還沒有完成：

- 93,525 rows 全量 Qwen shadow index。
- 完整 LanceDB 寫入、查詢、增量更新、重啟與回滾。
- 真實 OpenClaw 插件 provider 抽離與一鍵安裝器。
- 斷網安裝、下載續傳、磁碟不足、程序崩潰與升級／解除安裝。
- 長時間熱負載、同時查詢與建索引、不同客戶硬體等級。

因此本輪只放行「開始整合工程」，不放行 Production 切換。現行 Gemini 索引、設定與排程均未修改。

## 安全 closeout

- A01、A04、A07：`NOT_APPLICABLE_WITH_EVIDENCE`；本 POC 無新帳號、授權、密碼或對外 endpoint。
- A02：`PASS`；loopback only、Web UI 關閉、正式環境零變更、無 cloud fallback。
- A03：`PASS`；官方來源、固定 revision、模型 SHA-256 與授權均已核對。
- A05：`PASS`；固定參數、結構化 JSON、文件內容不作 shell 指令。
- A06：`PASS`；shadow corpus、資源限制、獨立模型與索引邊界。
- A08：`PASS`；模型雜湊、維度、有限值、norm 與 embedding identity 驗證。
- A09：`PASS`；提交證據不含 corpus 全文、向量或秘密，工具 log 會 redacted 本機 API key。
- A10：`BLOCKED`（正式發佈）；POC 已完成，但 lifecycle manager 尚未實作，且雙重 Ctrl-C cleanup assertion 必須納入 fault／restart 測試。
- AI Security Overlay：`PASS`；文字只送 embedding，無工具呼叫或模型輸出執行路徑。
- ASVS v5.0.0：`NOT_APPLICABLE_WITH_REASON`；本輪沒有對外 Web／API 產品介面。
- P0／P1：0；P2：2（graceful lifecycle／crash recovery、全量資源與查詢驗證）。
- `RELEASE_DECISION: BLOCK`；`POC_DECISION: PASS`。

## 可重現資訊

- Qwen GGUF revision：`f4602530db1d980e16da9d7d3a70294cf5c190be`。
- Q5 SHA-256：`9fd05563211c2d69d74abb8769fa92983a102d11575b2517a119b0037dff217c`。
- Q4 SHA-256：`2b0cf8f17b4c723c27303015383c27ec4bf2d8314bb677d05e920dd70bb0f16b`。
- llama.cpp revision：`f1357e49980f5462af9783164f3fdec407d90137`。
- 完整去識別摘要：[`2026-08-25-qwen-vs-gemini-poc-summary.json`](./2026-08-25-qwen-vs-gemini-poc-summary.json)。
- 測試設計：[`2026-08-25-qwen-gemini-embedding-poc-design.md`](../superpowers/specs/2026-08-25-qwen-gemini-embedding-poc-design.md)。
- 官方來源整理：[`2026-08-25-qwen-vs-gemini-official-sources.md`](../research/2026-08-25-qwen-vs-gemini-official-sources.md)。
- Apple Silicon 初步評估：[`2026-08-25-qwen3-embedding-4b-apple-silicon-evaluation.md`](../research/2026-08-25-qwen3-embedding-4b-apple-silicon-evaluation.md)。

官方來源：[Qwen 模型](https://huggingface.co/Qwen/Qwen3-Embedding-4B)、[Qwen GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF)、[llama.cpp](https://github.com/ggml-org/llama.cpp)、[Gemini embeddings](https://ai.google.dev/gemini-api/docs/embeddings)、[Gemini deprecations](https://ai.google.dev/gemini-api/docs/deprecations)。Gemini 官方目前列出的 2028-05-14 是最早可能退役日，不代表一定在該日立即關閉。
