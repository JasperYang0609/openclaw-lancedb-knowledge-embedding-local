# Qwen 全量 Shadow 驗證 Day 4 報告

日期：2026-08-31

狀態：`PASS_SHADOW_ONLY_WITH_P2`

## 結論

Qwen Day 4 隔離穩定性 Gate 通過。Qwen 與 Gemini 使用同一組 20 題執行 read-only 品質對照；Gemini query embedding 只讀取 33-row 隔離 cache，Production query cache、config、state、LanceDB 與 Qwen full shadow 在最終對照查詢前後的檔案數及 aggregate SHA-256 完全一致。Production provider 未切換。

唯一新 finding 為 `D4-01 P2`：full runner 已完成且 OS process 不存在，但 `runner.pid.json` 仍保留舊 PID metadata。這沒有造成殘留程序、重複 runner 或資料改寫，也不阻擋 Day 5；正式產品化前應讓成功／失敗／中斷 cleanup 一致移除或終態化 runner state。

## 週期性品質對照

- Qwen Q5 768：Hit@5 18／20（90%），MRR 0.7667，前後兩次 release-quality benchmark 結果一致，Gate PASS。
- Gemini 768：Hit@5 19／20（95%），MRR 0.7083，前後兩次 release-quality benchmark 結果一致，Gate PASS。
- 與 Day 2 相同：Qwen miss `local-11`、`local-20`；Gemini miss `local-20`。未發生品質漂移。
- Gemini 隔離 query cache 在測試前後均為 33 rows；Production cache 在最終對照前後均為 9,581 rows。
- Gemini 本輪延遲代表隔離 cache-hit read-only 路徑，不代表雲端 API latency；本輪沒有外送新 query 或新增 Production cache row。

## 端到端延遲與重複循環

測量邊界為 fresh CLI process → query embedding → LanceDB retrieval → rendered search output。每個 provider 執行 3 輪、每輪 20 題，共 60 次查詢。

- Qwen 各輪 p95：663.0、522.5、512.0 ms；60 次合併 p50 499.9 ms、p95 568.3 ms、max 809.4 ms，全部低於 1,000 ms Gate。
- Gemini 各輪 p95：218.4、218.6、251.6 ms；60 次合併 p50 196.4 ms、p95 251.6 ms、max 415.4 ms，全部低於 1,000 ms Gate。
- 兩個 provider 的 120 次查詢均成功，沒有 timeout、crash、重新啟動或品質結果漂移。

## 記憶體與長時間穩定性

- 同一個 Qwen sidecar PID 已持續運行約 24 小時；測試後 health＋embedding canary PASS。
- sidecar process physical footprint peak 687 MB；AGX `Alloc system memory` 由 6.12 GB 到 6.33 GB，低於 13.5 GiB Gate；AGX recovery count 為 0。
- 系統 memory free percentage 由 34% 到 30%；swap used 前後均為 3,389.12 MB，沒有新增 swap 使用或 swap runaway。
- 測試完成時只有 1 個 Qwen sidecar、1 個 loopback listener；active shadow runner 為 0。
- 主機本來已有較高 swap 使用量，雖本輪沒有增加，Day 5 仍應把此項列入獨立複核的環境風險觀察。

## 邊界不變證據

在最終 Qwen／Gemini 對照查詢前後，以相同的相對路徑＋檔案內容 deterministic aggregate SHA-256 比較：

- Gemini source config：1 file，unchanged。
- Gemini index state：1 file，unchanged。
- Gemini LanceDB：430 files，unchanged。
- Gemini embedding cache：1 file，unchanged。
- Qwen full shadow data：9,019 files，unchanged。

本輪只在 Day 4 隔離 root 寫入 query-only Gemini cache、benchmark／latency／memory 報告及 boundary digest；沒有修改 Production 或 full shadow table。

## Sidecar／runner 殘留檢查

- Sidecar：1 個受管程序、1 個 `127.0.0.1` listener、health＋embedding canary PASS；不是重複或無主殘留。
- Runner：active process 0；checkpoint 為 `complete`、96,163／96,163。
- `D4-01 P2 OPEN`：`runner.pid.json` 保留已死亡 PID。Day 5 應確認 lifecycle contract 後修復並補 success／failure／SIGKILL cleanup regression；修復不得觸碰 Production。

## OWASP Top 10:2025 任務範圍 Gate

- A01 PASS：Gemini config／state／table／cache與 Qwen full shadow final readback 全部 unchanged。
- A02 PASS：Qwen sidecar 僅有單一 loopback listener；Gemini 使用隔離 cache，沒有新 cloud query。
- A03 PASS：沿用已鎖定 model／runtime SHA-256、Q5 768 profile 與單一 embedding identity。
- A04 PASS：credential、query vector、corpus 與 raw result 未進 Git 或報告。
- A05 PASS：固定 20 題、固定 release threshold、隔離 cache、資源 Gate 與 deterministic boundary digest。
- A06 PASS：前後品質結果一致、120 次 latency query 無失敗、cache row count 不變。
- A07 `NOT_APPLICABLE_WITH_EVIDENCE`：本輪無登入、session 或使用者身分流程。
- A08 PASS：品質、latency、process、listener、checkpoint、cache row count 與 aggregate hashes 交叉驗證。
- A09 PASS：只提交去識別彙總與 evidence index；完整 log 保留於 workspace tool-run logs。
- A10 PASS：檢查長時間存活、重複負載、sidecar／runner process state、memory pressure、swap 與 GPU recovery。
- ASVS：`NOT_APPLICABLE_WITH_EVIDENCE`，本輪為本機 CLI／程序／資料完整性驗證，無 Web／公開 API。
- 攻擊者視角：不信任單一 PID file 或 checkpoint；以 OS process、listener、health canary、cache row count、品質結果與前後 aggregate SHA-256 交叉驗證。P0／P1 為 0；P2 為 1。

## 證據索引

- Day 4 隔離 root：`tmp/qwen-shadow-validation/day4/run-20260831-0906/`（不提交 Git）。
- 品質、三輪 latency、memory／swap／AGX、footprint 與 boundary evidence：隔離 root 的 `reports/`。
- 完整工具 logs：workspace `logs/tool-runs/` 內 `qwen-day4-*` runs（不提交 Git）。
- Git 僅提交本去識別報告；不提交 corpus、vectors、credential、query cache 或本機 runtime paths。

## 下一步

依核准五日流程進入 Day 5 獨立複核：重跑關鍵品質／性能／邊界 Gate、審查 `D4-01` 的嚴重度與修復範圍，交付是否可正式採用的結論。任何 Production 切換、Gemini 移除或正式索引替換仍需獨立 Human Gate。
