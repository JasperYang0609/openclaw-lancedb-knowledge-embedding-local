#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENCLAW_LANCEDB_ROOT:-$HOME/.openclaw/workspace/knowledge-lancedb}"
LOG_DIR="$ROOT/reports/cron-logs"
LOCK_DIR="$ROOT/data/index.lock"
mkdir -p "$LOG_DIR" "$ROOT/data"
STAMP="$(TZ=${TZ:-Asia/Taipei} date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/incremental-$STAMP.log"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[knowledge-index] another indexing run is active; skip" | tee "$LOG"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
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
  compact_cache_if_oversized || true
  rotate_reports || true
  echo "[knowledge-index] finished_at=$(date +%Y-%m-%dT%H:%M:%S%z)"
} 2>&1 | tee "$LOG"
