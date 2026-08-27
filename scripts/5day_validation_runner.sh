#!/bin/bash
set -e

export LANCEDB_SHADOW_ROOT="/tmp/qwen_shadow_lancedb"
export PYTHONPATH="$(pwd)"

echo "[$(date)] Starting 5-day isolation test validation..."
echo "[$(date)] Phase 1 & 2 tests (Isolation & Lifecycle)..."
python3 -m pytest tests/

echo "[$(date)] Day 1: Starting full shadow build in background (94,800 chunks)..."
python3 -c "from src.runner.shadow_builder import ShadowBuilder; b = ShadowBuilder(); b.build(); b.verify()"

echo "[$(date)] Checkpoint saved. Schedule armed for Days 2-5."
