# CI Push 降噪設計

本 repo 採 ansai 全域 CI 政策：開發分支 push 僅跑 `scripts/check_push.sh` 必要 Gate；pull request、main push 與手動觸發保留完整 CI；同一 branch／PR 的舊 run 自動取消；已有 open PR 時略過重複的 Branch Check，由 PR 完整 CI 驗證。

不關閉真正失敗通知，不新增寫入權限、秘密、第三方服務或產品／部署變更。完整 CI 仍保留全量 Python pytest、Plugin validate、snapshot、archive parity 與 dangerous-exec Gate。
