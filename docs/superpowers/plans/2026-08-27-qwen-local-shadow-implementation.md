# Qwen 本機 Embedding Shadow 實作計畫

## 階段 1：基礎保護與隔離 (Day 1)
- [ ] 1.1 建立環境變數覆寫機制，確保 Shadow runner 使用 `LANCEDB_SHADOW_ROOT`，完全不碰 Production DB。
- [ ] 1.2 建立 `tests/test_isolation.py` 驗證：
  - Shadow runner 嘗試存取 Production DB 時必須拋出例外。
  - Provider identifier 必須不等於 `gemini-embedding-001`。
- [ ] 1.3 實作 Qwen Provider (`src/providers/qwen_local.py`)，先作 Dummy 回傳，通過隔離測試。

## 階段 2：Sidecar 生命週期與安裝器 (Day 1)
- [ ] 2.1 實作 `src/lifecycle/llama_server_manager.py` (啟動、停止、健康檢查)。
- [ ] 2.2 實作 `src/installer/qwen_installer.py` (下載 GGUF, llama-server, 驗證 SHA-256)。
- [ ] 2.3 加入生命週期與安裝器的 Unit Tests。

## 階段 3：全量 Shadow 建立器 (Day 1)
- [ ] 3.1 實作 `src/runner/shadow_builder.py` (讀取來源快照，切批，儲存 checkpoint，斷點續傳)。
- [ ] 3.2 驗證對帳邏輯 (fingerprint, row count, chunk id 唯一性)。
- [ ] 3.3 加入對應的端到端（小規模）整合測試。

## 階段 4：啟動五日排程 (Day 1)
- [ ] 4.1 撰寫 5 日排程腳本 `scripts/5day_validation_runner.sh` 或 Python entrypoint。
- [ ] 4.2 開始啟動 Day 1 的全量資料索引 (94,800 chunks)，並在背景執行。
