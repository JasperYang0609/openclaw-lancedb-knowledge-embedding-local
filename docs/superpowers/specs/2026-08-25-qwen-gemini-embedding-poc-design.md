# Qwen 與 Gemini Embedding 隔離對照測試設計

日期：2026-08-25  
狀態：Jasper 已在 Discord 核准進行隔離測試  
目標：用安賽現有知識資料與 20 題人工基準，判斷本機 Qwen3-Embedding-4B 是否能在搜尋品質與速度上取代 Gemini Embedding。

## 範圍與不可變邊界

- 現行 Gemini LanceDB、設定、快取、排程與正式搜尋全部唯讀，不切換、不覆寫。
- Qwen 模型、測試索引、暫存與報告使用獨立目錄；失敗可直接移除，不影響正式環境。
- 不把模型檔提交到 Git；只記錄官方來源、固定版本與雜湊。
- 不把 API key、原始敏感內容、完整本機路徑或測試資料提交到公開 Repo。
- 本輪只回答「品質與本機可行性」，不實作正式安裝器、不發布、不部署。

## 比較方案

採固定 1,000 段 shadow corpus：完整保留 20 題指定答案來源的相關內容，再用固定規則補入干擾內容。相同 1,000 段與相同 20 題同時比較：

1. 現行 Gemini `gemini-embedding-001` 768 維向量。
2. Qwen3-Embedding-4B Q5_K_M 的 768 維輸出。
3. Qwen3-Embedding-4B Q5_K_M 的原生 2,560 維輸出。
4. 若時間與資源允許，再以 Q4_K_M 重跑 768 維，判斷較小模型檔是否有明顯退步。

Qwen 查詢使用官方建議的 retrieval instruction；文件不加查詢 instruction。所有向量在比較前做一致的 L2 normalization。

## 指標與判定

- 品質：Hit@5、MRR、每題命中排名、雙方各自贏／輸的題目。
- 效能：模型冷啟動、1,000 段建立時間、平均文件速度、20 題查詢 p50／p95、記憶體峰值。
- 正確性：輸出維度、有限數值、向量長度約為 1、重複查詢穩定性。
- 建議 Gate：Qwen Hit@5 不低於 Gemini 超過 5 個百分點，MRR 不低於 Gemini 超過 0.05，且本機查詢 p95 可接受；否則不建議取代。

本測試是方向性 POC，不等同 93,525 段全量壓力測試。若 POC 通過，下一階段才做完整 shadow index 與長時間穩定性驗證。

## 安全範圍與威脅模型

### SECURITY_SCOPE

- 資料分類：既有內部知識內容；正式索引唯讀，報告只保存彙總指標與去識別失敗案例。
- 信任邊界：官方 Qwen GGUF 下載、官方 llama.cpp 原始碼／執行檔、本機 loopback sidecar、隔離測試目錄。
- 外部服務與成本：模型／原始碼下載；Gemini 基準優先重用既有向量與快取，不新增正式資料外送。
- AI 工具：embedding only，無寫入外部服務、無工具執行能力。

### THREAT_MODEL

- 供應鏈替換：固定官方 revision，下載後驗 SHA-256。
- 本機服務誤曝露：只綁 `127.0.0.1`，測試後停止程序。
- 索引混用／污染：Gemini 與 Qwen 使用獨立資料結構與 embedding fingerprint。
- 敏感資料進報告／Git：只提交彙總結果、設定與可重現方法，不提交 corpus 或向量。
- 資源耗盡：限制 context、batch、並發與 1,000 段範圍；監測失敗並 fail closed。

### OWASP 2025 計畫

- A01、A04、A07：無新帳號、授權、密碼或正式 endpoint，`NOT_APPLICABLE_WITH_EVIDENCE`。
- A02：驗證 loopback-only、無 cloud fallback、正式設定零變更。
- A03：驗證官方來源、固定 revision、SHA-256、授權。
- A05：測試工具不把 corpus 或模型輸出拼接成 shell；路徑與設定固定於隔離目錄。
- A06：隔離、資源上限、fail-closed 與不切 Production 為核心設計控制。
- A08：模型與測試產物雜湊、embedding fingerprint、結果 schema 驗證。
- A09：log 不含秘密與完整內文；只保留必要摘要。
- A10：測試下載中斷、sidecar 啟動失敗、輸出異常與清理；失敗不得標為通過。
- AI Security Overlay：文件視為不可信文字，只送 embedding 模型，不解析為指令，不允許任何工具呼叫。
- ASVS v5.0.0：本輪沒有對外 Web／API；記錄為 `not_applicable_with_reason`，以本機程序與供應鏈控制作等價驗證。

## 交付

- 官方來源研究檔。
- 機器可行性與 Gemini／Qwen 完整對照報告。
- 原始彙總 JSON／CSV（不得包含 corpus 全文或秘密）。
- Git commit hash、測試證據與 remaining blockers。
