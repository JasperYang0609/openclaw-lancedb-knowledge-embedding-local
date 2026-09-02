#!/usr/bin/env bash
set -euo pipefail

: "${QWEN_CANDIDATE:?QWEN_CANDIDATE is required}"
: "${QWEN_ACTIVE_LINK:?QWEN_ACTIVE_LINK is required}"
: "${QWEN_RECEIPT_DIR:?QWEN_RECEIPT_DIR is required}"
: "${QWEN_ACTIVATOR:?QWEN_ACTIVATOR is required}"

case "$QWEN_CANDIDATE" in
  /*) ;;
  *) echo "QWEN_CANDIDATE must be absolute" >&2; exit 2 ;;
esac
test -d "$QWEN_CANDIDATE"
test ! -L "$QWEN_CANDIDATE"
test -f "$QWEN_CANDIDATE/config/source-map.json"

cd "$QWEN_CANDIDATE"
npm run index
npm run audit
node src/cli.js search "OpenClaw 客戶導入 checklist 固定入口" --limit 1 > reports/cutover-canary.txt
grep -q '^## 1\.' reports/cutover-canary.txt
python3 "$QWEN_ACTIVATOR" \
  --active-link "$QWEN_ACTIVE_LINK" \
  --candidate "$QWEN_CANDIDATE" \
  --receipt-dir "$QWEN_RECEIPT_DIR"

echo "QWEN_REBUILD_ACTIVATED"
