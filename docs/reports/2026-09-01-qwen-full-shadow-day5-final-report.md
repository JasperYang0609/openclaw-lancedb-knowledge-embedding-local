# Qwen 本機 Embedding 五日 Shadow 驗證最終報告

日期：2026-09-01

狀態：`PASS_SHADOW_ONLY_READY_FOR_CONTROLLED_CUTOVER_REVIEW`

## 一句話結論

Qwen3-Embedding-4B Q5_K_M／768 維已通過 96,163 筆全量索引、品質、速度、記憶體、48 小時 sidecar、故障恢復與兩輪安裝／解除安裝演練；建議進入「這台 Mac 的受控 Production 切換審查」，但不可直接切換，也不可宣稱客戶一鍵安裝包已完成。

## 五日結果

- 全量索引：96,163／96,163，unique IDs 96,163，duplicate 0，768 維 finite vectors 96,163，單一 embedding identity。
- 首次重建：14 小時 59 分，低於 20 小時 Gate。
- 最終 20 題品質：Qwen Hit@5 90%、MRR 0.7667；Gemini Hit@5 95%、MRR 0.7083。Qwen Hit@5 低 5 個百分點、未超過容許差距；MRR 高 0.0583。
- 最終端到端 p95：Qwen 744.7 ms；Gemini 隔離 cache-hit 302.7 ms；兩者都低於 1,000 ms Gate。Gemini 數字不代表雲端 cache-miss latency。
- 穩定性：同一 Qwen sidecar 已連續存活超過 48 小時；Day 4 process physical peak 687 MB、AGX allocation peak 約 6.33 GB，無新增 swap、AGX recovery 0。
- 故障測試：restart、SIGTERM、SIGKILL、stale PID、port collision、checkpoint resume、斷外網、增量新增／修改／刪除與重跑全部 PASS。
- 安裝演練：兩輪 fresh install → loopback health＋embedding canary → stop → uninstall → restore reinstall 全部 PASS；來源 artifact hash 保持不變。
- 完整測試：Python 22／22、Node 41／41、post-run 16／16；npm high／critical 0，Python dependency consistency PASS。

## Day 5 修復

- `D4-01 RESOLVED`：runner 的已死亡 PID metadata 現會依 checkpoint 終態化，並檢查 process command，避免 PID reuse 誤判。
- installer manifest identity 不再自行信任 manifest 內的 hash；必須符合程式固定 revision／SHA-256／受管路徑。
- uninstaller 只刪除明確 allowlist 的受管檔案；遇到未知檔案、symlink directory、symlink runtime、錯誤權限或 hash mismatch 全部 fail closed，不做部分刪除。

## Production 邊界

Day 5 開始到最終品質／延遲重跑後，以下 deterministic aggregate hash 與檔案數全部不變：Gemini config、index state、LanceDB、embedding cache及 96,163-row Qwen full shadow data。Gemini 最終 benchmark 使用 33-row 隔離 query cache，未新增 Production query cache row。Production provider 未切換。

## 尚未完成／限制

- `D5-01 P1 PRODUCTIZATION`：installer core 目前要求傳入已下載且 hash 已知的 model／llama-server source；遠端下載、續傳、暫存＋原子 rename及客戶-facing 單一 CLI 尚未實作。這不阻擋本機候選品質，但阻擋「客戶一鍵安裝包完成」宣告。
- `D5-02 P2 ENVIRONMENT`：系統 Python 3.9 使用 LibreSSL 2.8.3，urllib3 v2 會發出 TLS 支援警告。本輪 Qwen 僅走 loopback HTTP、無 cloud fallback，沒有影響測試；未來若由 Python downloader 走 HTTPS，需先更新 runtime 或採相容依賴。
- full shadow 是 2026-08-28 frozen corpus；正式切換前必須套用現況 delta 或重建最終索引，再重新做 row／fingerprint 對帳，不能把舊 shadow 直接改名上線。
- 第一版只驗證 macOS Apple Silicon；Windows、Linux、Intel Mac與客戶異質硬體未驗證。

## OWASP Top 10:2025 Final Gate

- A01 PASS：所有寫入限於 shadow／fresh rehearsal roots；Production 與 full shadow before／after hash 不變；uninstall fail closed。
- A02 PASS：單一 `127.0.0.1` listener、Web UI 關閉、無 cloud fallback、local credential 權限 0600。
- A03 PASS：model／runtime immutable revision＋SHA-256、單一 embedding identity、npm audit 0；下載產品化仍列 D5-01。
- A04 PASS：本機 credential 安全隨機、受限權限、不進 Git／報告／log。
- A05 PASS：路徑 specificity、manifest identity、URL loopback allowlist、hash／schema／finite vector及 symlink negative tests。
- A06 PASS：資源 Gate、atomic checkpoint、resume、idempotency、Production no-write boundary。
- A07 `NOT_APPLICABLE_WITH_EVIDENCE`：本輪無登入／session／使用者身分流程。
- A08 PASS：來源、rows、IDs、vectors、fingerprint、manifest、state與 before／after aggregate hash交叉驗證。
- A09 PASS：Git 只保存去識別彙總與程式；corpus、vectors、credential及完整敏感 log不提交。
- A10 PASS：下載來源缺失、hash mismatch、crash、timeout、port collision、stale PID、SIGKILL、resume、offline及 cleanup failure paths均 fail closed／可恢復。
- ASVS：`NOT_APPLICABLE_WITH_EVIDENCE`；本輪為本機 CLI／程序／資料完整性，無 Web／公開 API。
- P0／P1 security findings：0。產品完整性 P1：1（D5-01）。

## 建議決策

1. 可以核准下一階段「本機受控 cutover plan」：先更新 Qwen 索引至當前 corpus、再次 reconcile，保留 Gemini config／DB／cache作 rollback，切換後執行 canary 與觀察窗。
2. 未取得獨立 Human Gate 前，不修改 Production provider、不移除 Gemini、不刪除既有索引。
3. 客戶交付前先完成 D5-01，再做 fresh machine／非開發環境驗收；D5-02 隨 downloader runtime 一併處理。

## 證據索引

- Day 2：`docs/reports/2026-08-29-qwen-full-shadow-day2-report.md`
- Day 3：`docs/reports/2026-08-30-qwen-full-shadow-day3-report.md`
- Day 4：`docs/reports/2026-08-31-qwen-full-shadow-day4-report.md`
- Day 5 隔離證據：workspace `tmp/qwen-shadow-validation/day5/`（不提交 Git）。
- 完整工具 logs：workspace `logs/tool-runs/` 的 `qwen-day5-*` runs（不提交 Git）。
