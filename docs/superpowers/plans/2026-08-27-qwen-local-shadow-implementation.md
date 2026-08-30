# Qwen 本機 Embedding Shadow 實作計畫

狀態：`DAY_3_COMPLETE_SHADOW_ONLY`

## Day 1：產品路徑與全量重建

- [x] Git preflight、隔離分支與核准規格。
- [x] 真實 `qwen-local` provider：loopback only、本機 credential、2560→768、L2 normalization、fail closed。
- [x] 真實 macOS Apple Silicon 安裝器：artifact hash、固定 revision、權限、受管 manifest。
- [x] 真實 `llama-server` lifecycle：last pooling、health＋embedding canary、PID 與 port 防呆。
- [x] 真實 streaming shadow index：獨立 root/table、atomic checkpoint、resume、fingerprint 與 row reconciliation。
- [x] 10-row canary 實際寫入 LanceDB 並完成對帳。
- [x] 啟動 96,163-row frozen corpus 的背景全量重建；已驗證 PID、checkpoint 與 row growth。
- [x] 每小時巡檢、每 10% 里程碑／完成／異常才主動回報 Discord。

## Day 2：完成全量與品質對帳

- [x] 確認全量 terminal checkpoint 與 96,163 unique rows。
- [x] 驗證單一 embedding fingerprint、768 維、有限向量、來源／state／manifest 對帳。
- [x] 執行 20 題 Qwen／Gemini read-only benchmark 與端到端 latency。

## Day 3：故障與續跑

- [x] restart、SIGTERM、強制中斷、stale PID、port collision。
- [x] checkpoint resume 不重寫既有 rows。
- [x] 離線與增量 fixture 測試。

## Day 4：穩定性

- [ ] 週期性 read-only benchmark、查詢 p95、記憶體與長時間穩定性。
- [ ] 記錄異常、恢復與殘留程序／資源。

## Day 5：交付 Gate

- [ ] fresh reinstall／uninstall／restore rehearsal。
- [ ] OWASP A01–A10、供應鏈、secret、dependency 與攻擊者視角 review。
- [ ] 完整測試、Git closeout、白話報告與是否申請 Production 切換建議。

## 事故修正紀錄

2026-08-28 回讀發現先前 `d9cf6c7` 的 Day 1 runner 只有模擬輸出，沒有真實 Qwen embedding、背景程序、checkpoint row growth 或 Day 2–5 排程。該紀錄已撤回，mock provider／runner／shell 已移除。之後只有同時具備真實 PID、健康 canary、durable checkpoint 與資料筆數成長，才可稱為「已啟動」。

2026-08-29 Day 2 驗證發現既有 Gemini CLI benchmark 會在 cache miss 時追加 query embedding 到 Production cache。該 20 筆本輪新增列已先隔離備份，再依 exact query key 全數回復；其他同期新增列為 0，Production Gemini table／state／config 未變。Day 4 不得再直接使用 Production cache，必須改用隔離 query cache。

2026-08-30 Day 3 真實故障注入發現 lifecycle manager 對自己啟動的 child process 只用 PID polling，SIGTERM 後可能因未 reap 的 zombie 等到 kill timeout。已改為持有 `Popen` 時直接 `wait()`，逾時才 `kill()` 並再次 `wait()`；新增 regression test 並以真實 Qwen sidecar 驗證。Day 3 其餘 restart、強制中斷、stale PID、port collision、checkpoint resume 零重寫、斷外網查詢與新增／修改／刪除增量 fixture 全部通過。
