#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENCLAW_LANCEDB_ROOT:-$HOME/.openclaw/workspace/knowledge-lancedb}"
cd "$ROOT"
npm run search -- "$@"
