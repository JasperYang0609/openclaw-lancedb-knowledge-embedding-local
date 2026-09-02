#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENCLAW_LANCEDB_ROOT:-$HOME/.openclaw/workspace/knowledge-lancedb-qwen-local}"
LOCK_DIR="$ROOT/data/index.lock"
LOG_DIR="$ROOT/reports/cron-logs"
mkdir -p "$ROOT/data" "$LOG_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[knowledge-index] another indexing run is active; initial build skipped"
  exit 75
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT
cd "$ROOT"
STAMP="$(TZ=${TZ:-Asia/Taipei} date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/full-$STAMP.log"
{
  echo "[knowledge-index] full build started_at=$(date +%Y-%m-%dT%H:%M:%S%z)"
  npm run index
  node src/cli.js audit --mark-ready
  echo "[knowledge-index] full build finished_at=$(date +%Y-%m-%dT%H:%M:%S%z)"
} 2>&1 | tee "$LOG"
