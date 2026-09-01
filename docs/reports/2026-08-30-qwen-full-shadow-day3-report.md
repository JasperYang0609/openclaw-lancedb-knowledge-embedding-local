# Qwen 全量 Shadow 驗證 Day 3 報告

日期：2026-08-30

狀態：`PASS_SHADOW_ONLY`

## 結論

Qwen Day 3 隔離故障與續跑 Gate 已通過。真實 Qwen sidecar、真實 LanceDB 與 24-row 合成 fixture 完成 restart、SIGTERM、強制中斷、stale PID、port collision、checkpoint resume、斷外網查詢及新增／修改／刪除增量測試。這只代表候選可繼續 Day 4；Production provider 切換仍未核准。

## 真實故障與 lifecycle

- SIGTERM：程序正常退出、PID file 移除、無殘留 sidecar。
- Restart：第二次啟動取得新 PID，任一時間只有一個受管 sidecar。
- Stale PID：死亡 PID record 被安全辨識並替換。
- Port collision：loopback port 已被占用時 fail closed，未產生第二個 sidecar 或 PID file。
- 強制中斷：對 sidecar 執行 SIGKILL 後，下一次啟動辨識 stale PID 並健康恢復。
- 測試完成後原 Qwen shadow sidecar 已恢復；`127.0.0.1:18888` health＋embedding canary PASS，測試 port 無 listener。

## Checkpoint resume 與不重寫證據

- Runner 使用 batch size 1 建立真實 Qwen vectors；durable checkpoint 到 3 rows 時執行 SIGKILL。
- 中斷後 LanceDB 可見 3 durable rows；resume 後完成 24／24，unique IDs 24、duplicate 0。
- Resume 前 3 rows 的 row＋vector digest 完全一致，既有 row 重寫 0。
- 對 terminal checkpoint 再執行一次，24 rows digest 全部一致，完成態重跑 row 重寫 0。

## 斷網查詢與增量 fixture

- 使用 macOS sandbox 明確拒絕非 loopback outbound；外部 IP 連線失敗，本機 Qwen search 成功，無 cloud fallback。
- Fixture：新增 1、修改 1、刪除 1；完成後 24 rows／24 unique IDs。
- 未變更來源的 row＋vector digest 保持一致。
- 第二次 incremental：changed files 0、added chunks 0，table digest 不變，exactly-once final state PASS。

## 已修復 Finding

`D3-01 RESOLVED`：lifecycle manager 對自己啟動的 child process 在 SIGTERM 後未直接 `wait()`；程序可能成為未 reap zombie 並拖到 kill timeout。修正後，受管 child 先 SIGTERM＋`wait()`，只有逾時才 SIGKILL＋再次 `wait()`。回歸測試與真實 sidecar 多次啟停均通過。

## Production／全量 Shadow 不變邊界

Day 3 前後 aggregate SHA-256 與檔案數完全一致：

- Gemini source config：1 file，`4a756cc5eed2fc80f819f9dbffa02c9957e0764212feeea5ea2e6f75a7f7063e`。
- Gemini index state：1 file，`098a0c669fa6e1eb07524d510f38b448d0bd1bf1d8861841b88db64021a05a8a`。
- Gemini LanceDB：444 files，`2a7a33577a549a7b9099be4eaeb9f56b565fa9343caa0f38d0c0bdc98e351e7b`。
- Gemini embedding cache：1 file，`478b5e1425a7135f5f7d43349a2ce1e21456bd7711bd1ae0e1f3938eea5c420f`。
- 96,163-row Qwen full shadow：9,028 files，`65ab4278f0c51f8ff314043cd152ea776116c28a5a5213e96d1c6baa8c27c0be`。

因此 Production Gemini config／state／table／cache 與 Day 2 full shadow 均未被本輪改寫。

## OWASP Top 10:2025 任務範圍 Gate

- A01 PASS：所有 mutation 限於新建 fixture root；Production Gemini 與 full shadow 前後 digest 相同。
- A02 PASS：sidecar 只允許 loopback；斷外網 sandbox 內查詢成功且無 cloud fallback。
- A03 PASS：Qwen model／llama-server 以固定 SHA-256 回讀後執行。
- A04 PASS：本機 credential 未進 Git、報告或驗證輸出。
- A05 PASS：validation root、port、PID、endpoint、checkpoint schema 與 row schema均 fail closed 驗證。
- A06 PASS：resume、terminal rerun 與 incremental rerun 均維持 exactly-once final state。
- A07 `NOT_APPLICABLE_WITH_EVIDENCE`：本輪無登入、session 或使用者身分流程。
- A08 PASS：checkpoint、unique ID、row＋vector digest 與 Production boundary digest 交叉驗證。
- A09 PASS：只保留去識別彙總與 redacted log；不提交 corpus、vector、credential 或本機 raw evidence。
- A10 PASS：restart、SIGTERM、SIGKILL、stale PID、port collision、runner crash 與 resume 均有真實 fault injection。
- ASVS：`NOT_APPLICABLE_WITH_EVIDENCE`，本輪為本機 CLI／程序／資料完整性驗證，無 Web／公開 API。
- 攻擊者視角：不信任 PID file 或 checkpoint 單一聲明；以 OS process／port、LanceDB rows、row digests、external egress denial 與前後 boundary hashes 交叉驗證。P0／P1 為 0。

## 證據索引

- 可重跑 verifier：`scripts/qwen_day3_validation.py`。
- Lifecycle regression：`tests/test_qwen_installer_lifecycle.py`。
- 真實驗證彙總：隔離 root 內 `reports/day3-validation.json`；含本機 runtime 路徑與 PID，不提交 Git。
- 完整工具 log：workspace `logs/tool-runs` 內的 `qwen-day3-real-validation` run；不提交 Git。

## 下一步

依核准計畫進入 Day 4：使用隔離 Gemini query cache 執行週期性 Qwen／Gemini read-only benchmark、p95、記憶體與長時間穩定性測試。不得再次直接使用 Production Gemini query cache。
