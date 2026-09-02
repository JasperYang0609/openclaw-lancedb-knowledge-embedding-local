# OpenClaw 原生地端知識整合安裝器設計

日期：2026-09-02

狀態：`APPROVED_BY_JASPER_2026-09-02`

## 一句話目標

提供單一、可回滾的安裝流程，將 Qwen 地端 embedding、LanceDB、OpenClaw Plugin、Skill、常駐服務與增量排程整合起來，讓 OpenClaw 在需要既有專案與歷史資料時主動呼叫地端搜尋工具，且不再依賴 Gemini embedding。

## 已確認決策

- 架構採 OpenClaw Plugin＋Skill，不以模型臨時拼接 shell 指令作正式搜尋入口。
- 安裝完成並通過設定驗證後，安裝器可自動重啟 OpenClaw Gateway。
- 若既有 Gemini 版存在，停用其搜尋／增量排程，但保留 Skill、設定、索引與排程快照供緊急回滾。
- 第一版正式支援 macOS Apple Silicon、OpenClaw `2026.7.1-2` 或經相容性測試明確放行的版本。

## 使用者體驗

從 repository root 執行：

```bash
./qwen-local integrate-openclaw
```

此命令完成 preflight、備份、Qwen runtime 安裝、索引專案 bootstrap、Plugin 與 Skill 安裝、常駐服務與增量排程註冊、Gemini 排程停用、Gateway 重啟及 post-install 驗收。重複執行必須冪等，不產生重複 Plugin、Skill、服務或排程。

索引尚未 ready 時，OpenClaw 工具明確回傳 `INDEX_BUILDING`；不得查 Gemini、不得混用舊 provider 向量，也不得回傳假結果。索引完成 exact reconciliation 後才原子標記為 ready。

## 架構與元件

### 1. Integration orchestrator

- 擴充 repository-level `qwen-local` CLI，加入 `integrate-openclaw`、`verify-openclaw`、`rollback-openclaw` 與 `uninstall-openclaw`。
- 只透過固定 argv 呼叫 OpenClaw CLI；禁止 `shell=True`、字串拼接命令與未驗證的 executable path。
- 每個階段寫入受管 transaction manifest，保存狀態、建立的資產與回滾所需 metadata；不得保存 token、語料、向量或私密訊息。

### 2. OpenClaw Plugin

- Plugin id 固定為 `openclaw-lancedb-knowledge-local`。
- 註冊 `local_knowledge_search` 工具；輸入只接受 query、limit 與受限 project filter。
- 工具透過固定 Node executable／固定 project root／固定 CLI entry 執行搜尋，限制 timeout、stdout bytes、結果筆數與 query 長度。
- 工具輸出為穩定 JSON schema：狀態、結果摘要、source path、chunk identity 與 provider identity；不得輸出向量、credential 或未 redacted 的錯誤環境。
- Plugin 僅能讀取已驗證且 ready 的 Qwen index；不得接受任意 DB path、command、endpoint 或 cloud provider。

### 3. OpenClaw Skill

- 使用既有 `openclaw-lancedb-knowledge-local` Skill identity，透過 `openclaw skills install` 安裝至目標 agent workspace。
- Skill 明確要求：遇到過去決策、專案狀態、會議／備份紀錄、內部文件與需要來源佐證的本機資料時，主動使用 `local_knowledge_search`。
- 一般常識、創作或與內部資料無關的問題不強制搜尋，避免每回合濫用工具。
- 搜尋結果必須引用工具提供的 source path；無結果時誠實回報，不可補造來源。

### 4. Runtime service

- Qwen sidecar 使用 per-user launchd service，開機／登入後自動啟動，只綁 `127.0.0.1`。
- service 指向受管、hash-verified runtime 與 model；API credential 只存權限受限的本機檔案。
- lifecycle manager 與 launchd 必須共用單一 ownership contract，避免重複 sidecar、stale PID 或 port collision。

### 5. Index and schedule

- Bootstrap 固定 Qwen-specific project、table、cache、state 與 embedding identity，不覆寫 Gemini index。
- 初次全量索引可在背景續跑；checkpoint、row、unique ID、dimensions、finite values、provider／model／runtime fingerprint 全部對帳後才 ready。
- 以 OpenClaw 宣告式 command cron 建立唯一 declaration key 的每日增量任務；固定 argv、cwd、timeout、output cap 與 failure alert。
- 安裝器只停用能以受管 identity 精確辨識的 Gemini jobs；無法確認 ownership 時停止並要求人工處理，不按名稱模糊匹配。

## 安裝交易與回滾

1. Preflight：確認 OS／architecture／RAM／disk、OpenClaw CLI 與版本、Gateway、config validation、現有 Skill／Plugin／cron／service 狀態。
2. Snapshot：建立權限受限且不含 secrets 的設定與 ownership snapshot；記錄原 Gateway 狀態。
3. Stage：安裝／驗證 runtime、bootstrap project、stage Plugin／Skill／service／cron declarations。
4. Validate：Plugin manifest、Skill eligibility、tool allowlist、loopback health、index state與 OpenClaw config dry-run 全部通過。
5. Activate：啟用 Plugin／Skill／service／cron，精確停用 Gemini jobs，驗證設定後重啟 Gateway。
6. Acceptance：做 direct tool canary、fresh-session model-driven retrieval 與 no-not-to-search negative test。
7. Commit：全部通過才將 transaction 標為 committed；任一步失敗則逆序移除本次資產、恢復原 config／jobs／Gateway 狀態並再次驗證。

`uninstall-openclaw` 只移除 manifest-owned Qwen 整合資產。若安裝前存在 Gemini jobs，可依 snapshot 恢復；遇到未知檔案、ownership mismatch、符號連結或 config drift 時 fail closed，不部分刪除。

## 驗收標準

- `openclaw skills info openclaw-lancedb-knowledge-local` 顯示 eligible，來源為預期 workspace。
- `openclaw plugins inspect`／doctor 顯示 Plugin loaded，`local_knowledge_search` 工具存在且 owner 正確。
- launchd service 在登入重啟演練後只有一個受管 sidecar；loopback health＋embedding canary PASS。
- cron declaration key 唯一；重複安裝後仍只有一個增量 job，且 command argv／cwd／timeout／failure alert 完全符合規格。
- Direct tool canary 從測試 corpus 命中正確來源，無 Gemini endpoint、credential read 或 cloud fallback。
- 隔離 OpenClaw Profile 的 fresh session 執行 5 個自然語言、來源依賴問題；不提示搜尋工具，5/5 都必須由模型主動呼叫 `local_knowledge_search`、答對並附來源。
- 至少 2 個一般常識 negative prompts 不應無必要呼叫地端知識工具。
- Gateway restart、Qwen crash/restart、索引 building、empty result、corrupt result、timeout 與 cron failure 均有明確、redacted、fail-closed 行為。
- 安裝失敗注入與解除安裝演練均恢復 pre-install state；Gemini assets、來源文件及非受管 OpenClaw 設定不變。

## SECURITY_SCOPE

- `data_classification`：本機內部文件 D1／D2；credential、token、private key 永不索引或輸出。
- `trust_boundaries`：GitHub release／model artifacts、installer、OpenClaw CLI/config/Gateway、Plugin tool boundary、loopback sidecar、launchd、cron、corpus 與 LanceDB。
- `roles_and_tenants`：第一版單機單管理者；仍需防跨 project filter 越界與任意本機路徑讀取。
- `external_services_and_costs`：只在首次安裝下載鎖定 artifacts；搜尋與 indexing 不使用 cloud embedding。
- `ai_tools_and_write_capabilities`：模型只能呼叫 read-only `local_knowledge_search`；indexing 由固定 command job 寫入受管 Qwen project。

## OWASP Top 10:2025 與 ASVS 計畫

- A01：Plugin／installer／cron／service 僅操作 manifest-owned roots；跨路徑、跨 project 與 Gemini assets negative tests。
- A02：loopback-only、no Web UI、no cloud fallback、最小 tool allowlist、權限受限 state 與 launchd plist。
- A03：model/runtime/Plugin package pin＋hash、lockfile、license inventory、dependency audit 與 action SHA pinning。
- A04：credential 使用安全隨機值與受限檔案權限；log／report／Git 不輸出秘密。
- A05：query／limit／filter schema、argv execution、path allowlist、JSON output validation 與 hostile input tests。
- A06：transaction、idempotency、readiness gate、resource limits、rollback、no automatic Gemini fallback。
- A07：無產品登入介面，標 `NOT_APPLICABLE_WITH_EVIDENCE`；本機 ownership 與 OpenClaw agent boundary 仍測 A01／A04。
- A08：artifact、manifest、Plugin ownership、index fingerprint、row integrity 與 config snapshot integrity。
- A09：僅記錄 redacted install phase、health、run id、row counts、failure category；不記 corpus、query body、向量或 secrets。
- A10：download／service／Gateway／Plugin／cron／index partial failure、timeout、duplicate run、config drift 與 rollback fault injection。
- `AI_SECURITY_OVERLAY`：required；搜尋結果視為不可信資料，只可作回答依據，不得把 corpus 內容當指令執行。
- `ASVS_LEVEL_TARGET`：本輪無公開 Web/API，採 `not_applicable_with_reason`；Plugin tool schema、authorization boundary、輸入輸出與安全設定以等價 requirement register 留證。

## 非目標

- 不改 OpenClaw 核心記憶引擎原始碼。
- 不把 Qwen 宣稱為每一類問題都必須使用的工具。
- 不支援 Intel Mac、Linux 或 Windows 第一版正式安裝。
- 不刪除 Gemini Skill、index、cache 或設定；只精確停用受管 jobs。
- 不在本規格核准前發布、合併、修改目前 Production 或重啟 Gateway。

## 完成定義

程式、文件、deterministic package、OWASP Gate、實機安裝／回滾與 fresh-session 5/5 自主召回全部 PASS，獨立 review 無 P0／P1，GitHub PR／main CI 通過，README 明確寫出自動串接範圍與限制，才可宣稱「OpenClaw 安裝後會自主使用地端知識搜尋」。
