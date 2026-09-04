#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENCLAW_LANCEDB_ROOT:-$HOME/.openclaw/workspace/knowledge-lancedb-qwen-local}"
OWNERSHIP_MANIFEST="${1:-${QWEN_OWNERSHIP_MANIFEST:-$HOME/Library/Application Support/OpenClaw/qwen-local-integration/transaction.json}}"
PYTHON_BIN="${QWEN_PYTHON:-$(command -v python3)}"
LOG_DIR="$ROOT/reports/cron-logs"
LOCK_DIR="$ROOT/data/index.lock"
LOCK_HELPER="$ROOT/scripts/index_lock.py"
LOCK_ID=""
mkdir -p "$LOG_DIR" "$ROOT/data"
STAMP="$(TZ=${TZ:-Asia/Taipei} date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/incremental-$STAMP.log"
case "$PYTHON_BIN" in /*) ;; *) echo "[knowledge-index] Python executable must be absolute" >&2; exit 64 ;; esac
write_health() {
  "$PYTHON_BIN" "$ROOT/scripts/backup_health_component.py" \
    --ownership-manifest "$OWNERSHIP_MANIFEST" --event incremental --status "$1"
}
lock_rc=0
LOCK_ID="$("$PYTHON_BIN" "$LOCK_HELPER" acquire --lock "$LOCK_DIR")" || lock_rc=$?
case "$lock_rc" in
  0) ;;
  75)
    echo "[knowledge-index] another indexing run is active; skip" | tee "$LOG"
    exit 0
    ;;
  *)
    echo "[knowledge-index] index lock is unsafe or unavailable" | tee "$LOG" >&2
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

# Auto-compact the embedding cache when it exceeds 200MB (rewrites the JSONL only; no API calls).
compact_cache_if_oversized() {
  local cache_rel cache_file cache_bytes
  cache_rel="$(node -p "JSON.parse(require('fs').readFileSync('config/source-map.json','utf8')).embedding.cachePath || ''" 2>/dev/null || echo '')"
  [ -n "$cache_rel" ] || return 0
  case "$cache_rel" in
    /*) cache_file="$cache_rel" ;;
    *) cache_file="$ROOT/${cache_rel#./}" ;;
  esac
  [ -f "$cache_file" ] || return 0
  cache_bytes="$(stat -f%z "$cache_file" 2>/dev/null || stat -c%s "$cache_file" 2>/dev/null || echo 0)"
  if [ "$cache_bytes" -gt $((200 * 1024 * 1024)) ]; then
    echo "[knowledge-index] embedding cache ${cache_bytes} bytes > 200MB; running compact-cache"
    npm run compact-cache
  fi
}

# Rotate reports: keep the 14 most recent manifests and cron logs; *.latest.json is always kept.
rotate_reports() {
  local keep=14 pattern
  for pattern in "incremental-manifest.2*.json" "index-manifest.2*.json" "source-scan.2*.json"; do
    ls -1t "$ROOT/reports/"$pattern 2>/dev/null | tail -n +$((keep + 1)) | while IFS= read -r f; do rm -f "$f"; done || true
  done
  ls -1t "$ROOT/reports/cron-logs/"incremental-*.log 2>/dev/null | tail -n +$((keep + 1)) | while IFS= read -r f; do rm -f "$f"; done || true
}

{
  echo "[knowledge-index] started_at=$(date +%Y-%m-%dT%H:%M:%S%z)"
  npm run incremental
  node src/cli.js audit --mark-ready
  compact_cache_if_oversized || true
  rotate_reports || true
  write_health ok
  echo "[knowledge-index] finished_at=$(date +%Y-%m-%dT%H:%M:%S%z)"
} 2>&1 | tee "$LOG"
