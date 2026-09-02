# OpenClaw 原生地端知識整合實作計畫

日期：2026-09-02

狀態：`IMPLEMENTATION_PLAN_READY`

設計依據：`docs/superpowers/specs/2026-09-02-openclaw-native-local-knowledge-integration-design.md`

任務依據：`docs/tasks/2026-09-02-openclaw-native-local-knowledge-integration.md`

來源 exact commit：`7791d686e236ed2e89443068198b9507eebd6103`

## 完成目標

交付可重複執行、可驗證、可完整回滾的 OpenClaw integration layer。安裝後 Agent 取得真正的 `local_knowledge_search` 工具；Skill 明確規定何時主動查詢與引用來源；Qwen sidecar、Qwen index 與每日增量皆使用獨立受管 identity，不觸碰 Gemini index。

## Phase 0：契約與測試骨架

- 凍結 OpenClaw `2026.7.1-2` CLI 能力：Plugin install／inspect、Skill install／info、config validate、cron declaration、Gateway restart／status。
- 建立 fake OpenClaw CLI harness，所有 destructive/config tests 先在 temp profile 執行。
- 先寫失敗測試：任意 argv／path、symlink、hardlink、unknown config、重複 install、partial failure、rollback drift、stdout overflow、timeout、invalid JSON、Gemini fallback。
- 建立 package／manifest／tool contract tests，要求 `local_knowledge_search` owner 唯一且 schema closed。

## Phase 1：Plugin 與搜尋邊界

- 建立 `plugin/openclaw-lancedb-knowledge-local` package，固定 id 與 tool contract。
- Plugin 只從 operator-controlled config 取得固定 project root、Node executable與 ready-state path；啟動時 canonicalize 並驗證 allowlisted managed root。
- `query` 最大 2,000 字元；`limit` 1–10；`project` 只能是 manifest 中列出的 slug。
- 以 `spawn` fixed argv 執行 Node CLI，無 shell，設定 timeout、stdout／stderr cap、環境 allowlist。
- JSON parser 驗證 status、result count、source、chunk id、provider identity；移除向量、秘密、stack、本機 root prefix。
- `INDEX_BUILDING`、empty、timeout、corrupt／oversized output 皆穩定 fail closed，絕不觸發 Gemini。

## Phase 2：OpenClaw integration transaction

- 新增 `OpenClawCli` adapter：只允許固定 command templates；支援 profile／workspace 注入供 isolated test。
- 新增 transaction manifest：run id、schema、phase、pre-state hashes、owned assets、cron ids、Gateway prior state；原子 0600 寫入，不存 secrets。
- Preflight 驗證 platform、resources、OpenClaw exact／allowed version、Gateway/config、目標 paths、Plugin／Skill／cron／service collision。
- Snapshot 只保存必要 config／job metadata與 file hashes；若來源可能含 secret，保留權限受限本機備份而不複製內容到 manifest/log。
- Stage／activate／accept／commit 每一步可重入；故障逆序 compensation，rollback 本身需二次驗證。

## Phase 3：Skill、launchd 與 cron

- 強化 Skill：歷史決策、專案狀態、會議／備份、內部文件、來源佐證問題必須先用 tool；一般知識／創作不使用；結果內容永不作指令。
- 產生 per-user launchd plist：固定 Label、ProgramArguments、WorkingDirectory、KeepAlive／RunAtLoad、stdout／stderr 受管 path；禁止 shell、LAN bind、任意 env。
- lifecycle manager 與 launchd 共享 ownership marker；已有非受管同 label／port 時停止。
- 建立唯一 `declaration-key` command cron，固定 argv、cwd、timeout、no-output timeout、output cap；重跑更新／驗證同一 job，不複製。
- Gemini job 只在 declaration key／exact argv／known root 三項皆匹配時停用；否則保留並 BLOCK activation。

## Phase 4：CLI 與安裝體驗

- 擴充 `qwen-local`：`integrate-openclaw`、`verify-openclaw`、`rollback-openclaw`、`uninstall-openclaw`。
- `integrate-openclaw` 順序：preflight → snapshot → runtime install／health → bootstrap project → stage Plugin／Skill／service／cron → validate → activate → Gateway restart → acceptance → commit。
- `verify-openclaw` 僅讀：核對 manifest、owner、Plugin/Skill、service、cron、Gateway、sidecar、index readiness。
- `rollback-openclaw` 恢復最近一次 committed/prepared transaction 的 pre-state；`uninstall-openclaw` 只移除 manifest-owned Qwen assets並可恢復原 Gemini job。
- 所有 CLI output 使用 redacted JSON schema，exit code 可機器判讀。

## Phase 5：deterministic 驗證

- Python 全測試與 Node Plugin tests。
- template Node tests、postrun、snapshot、archive parity、dangerous-exec、no-cloud source scan。
- fault injection：每個 transaction phase、Gateway restart、Plugin install、Skill install、launchd load、cron add、direct canary。
- attacker review：path／command injection、project escape、prompt injection corpus、manifest forgery、config drift、PID/port collision、symlink/hardlink、oversized/corrupt child output、partial cleanup。
- 完成 OWASP A01–A10 matrix、等價 ASVS register、dependency／license／secret／package scans。

## Phase 6：隔離 OpenClaw Profile 驗收

- 使用 temp state、workspace、port、corpus與 isolated Gateway；不得修改正式 profile。
- 安裝兩次，驗證 Plugin／Skill／service／cron 仍各一份。
- direct tool canary 命中固定 fixture 並附 source；INDEX_BUILDING／empty／timeout negative tests。
- fresh session 5 個內部資料 prompt：5/5 自主 tool call、答對且引用來源。
- 2 個一般常識 prompt：不得呼叫本工具。
- 模擬 runtime crash／Gateway restart／uninstall／rollback，回讀所有 pre-state。

## Phase 7：本機受控安裝

- 重新 preflight 正式 profile，保存 config、Plugin／Skill／cron、Gateway與 Gemini asset hashes。
- 安裝 integration layer；若最新 Qwen index 尚 building，Plugin 對外只回 INDEX_BUILDING，直到 audit 後原子 ready。
- config validate 後重啟 Gateway，核對 tool owner、Skill eligibility、launchd 單實例、cron declaration。
- direct canary 後開 fresh session做 5＋2 prompt 驗收；任何一題不符合即 rollback，不宣稱完成。
- 驗收後確認 Gemini embedding jobs 停用，Gemini assets hashes不變。

## Phase 8：發布 closeout

- README 首屏說明安裝後的自動串接、自主使用條件、平台與 no-cloud 限制；加入 upgrade／verify／rollback／uninstall。
- 重建 deterministic skill／plugin artifacts並驗 source parity。
- 每個風險域分 commit；push feature branch，建立 PR，等待 CI。
- review exact commit 的 diff、tests、安全 Gate、package inventory與 production evidence；P0／P1=0 才 merge main。
- 最終回讀 GitHub visibility、main、CI、merge commit；本機 repo worktree clean。

## 預計 commit 邊界

1. `docs: add native OpenClaw integration implementation plan`
2. `test: define native integration and plugin contracts`
3. `feat(plugin): register local knowledge search tool`
4. `feat(integration): add transactional OpenClaw installer`
5. `feat(runtime): add launchd and incremental cron integration`
6. `docs: close native integration security and release evidence`
7. review／fix commits（若需要）

## 完成條件

- 全部 deterministic 與實機 Gate PASS。
- fresh-session 自主召回 5/5、一般常識不濫用 2/2。
- rollback／uninstall 確認非受管設定與 Gemini assets 不變。
- OWASP A01–A10 每項有證據，P0／P1=0。
- commit、PR、main CI、README、package parity與 clean worktree 全部可回讀。
