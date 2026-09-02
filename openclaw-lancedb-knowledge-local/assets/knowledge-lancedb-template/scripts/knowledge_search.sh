#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENCLAW_LANCEDB_ROOT:-$HOME/.openclaw/workspace/knowledge-lancedb-qwen-local}"
cd "$ROOT"
npm run search -- "$@"
