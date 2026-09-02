# OpenClaw 原生地端知識整合任務

日期：2026-09-02

狀態：`APPROVED_FOR_IMPLEMENTATION`

設計依據：`docs/superpowers/specs/2026-09-02-openclaw-native-local-knowledge-integration-design.md`

來源 commit：`7791d686e236ed2e89443068198b9507eebd6103`

## 目標

在 macOS Apple Silicon 上，以單一 `qwen-local integrate-openclaw` 命令完成 OpenClaw Plugin、Skill、Qwen launchd service、Qwen 專用 LanceDB project、每日增量 command cron、Gateway restart 與驗收；若任一步失敗，逆序恢復安裝前狀態。

## 授權範圍

- 可新增並啟用 read-only `local_knowledge_search` OpenClaw Plugin tool。
- 可安裝／更新 `openclaw-lancedb-knowledge-local` Skill。
- 可建立 per-user Qwen launchd service與一個具 declaration key 的每日增量 cron。
- 可精確停用本套件可證明 ownership 的 Gemini 增量 jobs，但不得刪除 Gemini Skill、設定、cache、index 或來源資料。
- 可在 config validate 通過後重啟 Gateway；失敗必須回滾。
- 可在隔離 profile 與本機正式 profile 執行已簽收的 direct／model-driven read-only 驗收。

## 禁止事項

- 不修改 OpenClaw core source、不新增 cloud embedding fallback。
- 不接受任意 executable、command、endpoint、DB path 或未受限 source path。
- 不把 token、API key、query body、corpus、向量、私密訊息或本機敏感路徑寫入 Git／log／manifest／回報。
- 不刪除非 manifest-owned 資產；ownership、symlink、hardlink、config drift 或 schema 不明時 fail closed。
- 不發布 release、不 merge main，除非本機與 GitHub Gate 全部通過且 P0／P1 為 0。

## 主要產物

- `plugin/openclaw-lancedb-knowledge-local/`：Plugin package、manifest、tool implementation 與測試。
- `src/openclaw_integration/`：preflight、snapshot、transaction、launchd、cron、OpenClaw CLI adapter、rollback／uninstall。
- `scripts/qwen_local.py`：四個 OpenClaw integration commands。
- `openclaw-lancedb-knowledge-local/SKILL.md`：主動搜尋條件與不濫用規則。
- deterministic tests、隔離 profile harness、OWASP closeout、README 與 package inventory。

## 驗收證據

- Python／Node unit、integration、fault-injection、archive parity、dependency、dangerous-exec、secret／cloud-boundary scans。
- isolated profile：Plugin owner、Skill eligibility、cron/service 唯一性、Gateway restart、direct tool canary。
- fresh session：5 個來源依賴 prompt 皆主動呼叫工具並答對、附來源；2 個一般常識 prompt 不呼叫工具。
- rollback／uninstall：pre-install config、jobs、Gateway state 恢復，Gemini 與非受管資產 hash 不變。
- production：只在以上 Gate 通過後受控安裝，重啟後再做 tool／session canary。

## SECURITY_SCOPE

- `data_classification`：本機 D1／D2 文件；秘密、憑證、私鑰、token 永不索引或輸出。
- `trust_boundaries`：GitHub artifacts、installer、OpenClaw CLI/config/Gateway、Plugin tool、loopback Qwen sidecar、launchd、cron、LanceDB、corpus。
- `roles_and_tenants`：單機單管理者；project filter 與 path 仍 deny-by-default。
- `external_services_and_costs`：首次下載固定 artifact；index／query 不呼叫 cloud embedding。
- `ai_tools_and_write_capabilities`：Agent 只取得 read-only search tool；寫入只由固定 indexing job 操作受管 Qwen project。

## OWASP_2025_PLAN

- A01：受管 root、project allowlist、Gemini／跨路徑 negative tests。
- A02：loopback-only、Web UI 關閉、最小 tool allowlist、0600／0700 權限、config validate。
- A03：Plugin／runtime／model identity、SHA-256、lockfile、package inventory、dependency audit。
- A04：安全隨機 sidecar credential、秘密不入 log／Git、受限權限。
- A05：query／limit／project schema、fixed argv、stdout／timeout cap、hostile input tests。
- A06：transaction、idempotency、readiness、resource caps、no Gemini fallback、rollback。
- A07：`NOT_APPLICABLE_WITH_EVIDENCE`；無新增登入／session，local ownership 由 A01／A04 覆蓋。
- A08：artifact hash、Plugin ownership、manifest/config snapshot integrity、index identity與 row reconciliation。
- A09：只記 phase／run id／counts／failure category；redaction test，無 query body、corpus、vector、secret。
- A10：download／service／Gateway／Plugin／cron／index failure、timeout、duplicate、drift、rollback injection。
- `business_logic_abuse_cases`：重複安裝、重複 cron/service、錯誤 project、config drift、假 Plugin owner、索引 building、超長 query、惡意 filter、解除安裝指錯 root。
- `AI_SECURITY_OVERLAY`：required；RAG 內容視為不可信資料，永不轉成 command/tool arguments。
- `ASVS_LEVEL_TARGET`：`not_applicable_with_reason`；無公開 Web/API，改以 local tool schema／process／filesystem 等價 register 留證。
- `HUMAN_SECURITY_GATES`：公開 release／main merge、未來非 Apple Silicon 平台、刪除 Gemini 資產仍需獨立核准。

## 停止條件

- worktree 出現未知變更、OpenClaw CLI 版本／介面不相容或正式設定 ownership 不明。
- Plugin 不能被 runtime 載入、tool owner 不唯一、Skill 不能 eligible、cron/service 無法冪等。
- 任何步驟需降低 fail-closed、rollback、no-cloud 或 5/5 自主召回 Gate。
- P0／P1 未關閉、秘密疑似外洩、Production 非受管設定或 Gemini 資產發生非預期變更。
