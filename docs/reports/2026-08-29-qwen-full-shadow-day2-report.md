# Qwen 全量 Shadow 驗證 Day 2 報告

日期：2026-08-29

狀態：`PASS_SHADOW_ONLY`

## 結論

Qwen 全量 shadow index 已完成且通過 Day 2 Gate。結果只證明隔離候選可繼續 Day 3～5；不代表已核准切換 Production Gemini。

## 全量與完整性

- Terminal checkpoint：`complete`，96,163／96,163。
- LanceDB：96,163 rows、96,163 unique chunk IDs、duplicate 0。
- Vector：schema 768 維；96,163 筆全部 768 維且 finite；non-finite value 0。
- Embedding identity：單一 provider／model／dimension identity；checkpoint、state、manifest 與設定 fingerprint 完全一致。
- Frozen corpus：table content fingerprint、checkpoint、state、manifest 完全一致。
- Source/state：4,675 個有 eligible chunks 的來源檔案及 file hash 全部對帳；mismatch 0。
- Live source drift：完成後來源新增 8 份文件、179 chunks；frozen IDs 遺失 0。此 drift 不混入 Day 2 frozen benchmark，後續由增量測試處理。

## 20 題品質 Benchmark

- Qwen Q5 768：Hit@5 18／20（90%），MRR 0.7667，release gate PASS。
- Gemini 768：Hit@5 19／20（95%），MRR 0.7083，release gate PASS。
- 差異：Qwen Hit@5 低 5 個百分點，未超過允許差距；Qwen MRR 高 0.0583。
- Qwen miss：`local-11`、`local-20`；Gemini miss：`local-20`。

## 端到端 Latency

測量邊界為 fresh CLI process → query embedding → LanceDB retrieval → rendered search output，共 20 題。

- Qwen：p50 552.7 ms、p95 634.4 ms、mean 570.2 ms、max 777.1 ms，p95 ≤ 1,000 ms PASS。
- Gemini：p50 581.9 ms、p95 660.8 ms、mean 607.9 ms、max 1,027.0 ms，p95 ≤ 1,000 ms PASS。

## Production 不變邊界與已修復 Finding

- Production Gemini table 保持 96,163 rows／768 維；table files、index state 與 config 在本輪沒有變更。
- `D2-01 RESOLVED`：既有 Gemini CLI 在 query cache miss 時會追加 Production cache。本輪精確辨識 20 筆、其他同期列 0；先做隔離備份後只移除該 20 筆，回讀殘留 0。
- Day 4 必須使用隔離 Gemini query cache；不得再次直接寫入 Production cache。

## OWASP Top 10:2025 任務範圍 Gate

- A01 PASS：shadow 路徑／table 隔離；Production table、state、config 不變；cache finding 已回復。
- A02 PASS：sidecar 只監聽 `127.0.0.1`，單一程序，無 cloud fallback。
- A03 PASS：model／runtime revision、hash、quantization 與 embedding identity 單一且對帳。
- A04 PASS：credential 未進報告、log 或 Git；本輪沒有新 credential。
- A05 PASS：row、ID、dimension、finite vector、file hash 與指紋均做 deterministic validation。
- A06 PASS：exact count、duplicate 0、source drift 分離，未用近似數字放行。
- A07 `NOT_APPLICABLE_WITH_EVIDENCE`：本輪無登入、session 或使用者身分流程。
- A08 PASS：table content fingerprint 與 checkpoint／state／manifest 一致。
- A09 PASS：保留去識別指標與 redacted log；不提交 corpus、vector 或秘密。
- A10 PASS：runner exit 0、terminal checkpoint complete；未因完成 watcher 重複寫入或重建。
- ASVS：`NOT_APPLICABLE_WITH_EVIDENCE`，本輪為本機 CLI／資料完整性驗證，無 Web／公開 API。
- 攻擊者視角：不只信任 runner manifest，另以 table row/content hash、state IDs、file hash、schema 與全量 finite scan 交叉驗證；偽造單一完成檔不足以通過 Gate。

## 下一步

依核准計畫進入 Day 3：restart、SIGTERM、強制中斷、stale PID、port collision、checkpoint resume、離線與增量 fixture。Production provider 切換仍需獨立 Human Gate。
