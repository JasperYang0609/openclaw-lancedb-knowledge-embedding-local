#!/bin/sh
set -eu

repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$repo_root"

python3 -m py_compile \
  scripts/build_skill_archive.py \
  scripts/check_dangerous_exec.py \
  scripts/qwen_local.py \
  src/openclaw_integration/core.py \
  src/openclaw_integration/launchd.py \
  tests/test_ci_workflow_contract.py

python3 -m pytest -q \
  tests/test_bootstrap_security.py \
  tests/test_openclaw_integration_contracts.py \
  tests/test_skill_archive.py \
  tests/test_ci_workflow_contract.py

(
  cd plugin/openclaw-lancedb-knowledge-local
  npm ci --ignore-scripts
  npm audit --omit=dev
  npm test
)

(
  cd openclaw-lancedb-knowledge-local/assets/knowledge-lancedb-template
  npm ci --ignore-scripts
  npm test
  npm run postrun:check
)

python3 scripts/check_dangerous_exec.py
python3 scripts/build_skill_archive.py --check
