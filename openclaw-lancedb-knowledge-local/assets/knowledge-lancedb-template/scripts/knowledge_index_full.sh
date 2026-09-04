#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENCLAW_LANCEDB_ROOT:-$HOME/.openclaw/workspace/knowledge-lancedb-qwen-local}"
OWNERSHIP_MANIFEST="${1:-${QWEN_OWNERSHIP_MANIFEST:-$HOME/Library/Application Support/OpenClaw/qwen-local-integration/transaction.json}}"
PYTHON_BIN="${QWEN_PYTHON:-$(command -v python3)}"
LOCK_DIR="$ROOT/data/index.lock"
LOCK_HELPER="$ROOT/scripts/index_lock.py"
LOCK_ID=""
LOG_DIR="$ROOT/reports/cron-logs"
mkdir -p "$ROOT/data" "$LOG_DIR"
case "$PYTHON_BIN" in /*) ;; *) echo "[knowledge-index] Python executable must be absolute" >&2; exit 64 ;; esac
write_health() {
  "$PYTHON_BIN" "$ROOT/scripts/backup_health_component.py" \
    --ownership-manifest "$OWNERSHIP_MANIFEST" --event initial --status "$1"
}
lock_rc=0
LOCK_ID="$("$PYTHON_BIN" "$LOCK_HELPER" acquire --lock "$LOCK_DIR")" || lock_rc=$?
case "$lock_rc" in
  0) ;;
  75)
    echo "[knowledge-index] another indexing run is active; initial build skipped"
    exit 75
    ;;
  *)
    echo "[knowledge-index] index lock is unsafe or unavailable" >&2
    write_health error >/dev/null 2>&1 || true
    exit "$lock_rc"
    ;;
esac
cleanup() {
  rc=$?
  trap - EXIT
  release_rc=0
  "$PYTHON_BIN" "$LOCK_HELPER" release --lock "$LOCK_DIR" --identity "$LOCK_ID" \
    >/dev/null 2>&1 || release_rc=$?
  if [ "$release_rc" -ne 0 ]; then
    rc=74
  fi
  if [ "$rc" -ne 0 ]; then
    write_health error >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT
cd "$ROOT"
STAMP="$(TZ=${TZ:-Asia/Taipei} date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/full-$STAMP.log"
{
  echo "[knowledge-index] full build started_at=$(date +%Y-%m-%dT%H:%M:%S%z)"
  npm run index
  node src/cli.js audit --mark-ready
  write_health ok
  echo "[knowledge-index] full build finished_at=$(date +%Y-%m-%dT%H:%M:%S%z)"
} 2>&1 | tee "$LOG"
