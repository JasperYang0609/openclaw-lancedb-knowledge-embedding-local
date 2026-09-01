import json
from pathlib import Path

import pytest

from unittest.mock import patch

from scripts.qwen_shadow_validation import (
    managed_paths,
    reconcile_runner_state,
    resolve_specific,
    write_shadow_config,
)


def test_resolve_specific_rejects_broad_paths():
    with pytest.raises(ValueError):
        resolve_specific("/")


def test_generated_config_is_qwen_local_and_cannot_overlap_production(tmp_path: Path):
    production = tmp_path / "production"
    project = tmp_path / "validation" / "project"
    shadow = tmp_path / "validation" / "shadow"
    runtime = tmp_path / "validation" / "runtime"
    (production / "config").mkdir(parents=True)
    (production / "config" / "source-map.json").write_text(json.dumps({
        "dbPath": "./data/lancedb",
        "tableName": "knowledge_chunks",
        "embedding": {"provider": "google-gemini"},
        "sources": [{"id": "raw", "sourceType": "discord_raw", "root": str(tmp_path)}],
    }))
    write_shadow_config(
        production_project=production,
        project=project,
        shadow_root=shadow,
        runtime_root=runtime,
        port=18888,
    )
    generated = json.loads((project / "config" / "source-map.json").read_text())
    assert generated["embedding"]["provider"] == "qwen-local"
    assert generated["embedding"]["pooling"] == "last"
    assert generated["embedding"]["dimensions"] == 768
    assert generated["privacy"]["cloudFallback"] == "DISABLED"
    assert generated["privacy"]["discordRawApproval"] == "LOCAL_ONLY"
    assert str(production.resolve()) in generated["shadow"]["forbiddenPaths"]
    assert generated["dbPath"].startswith(str(shadow.resolve()))

    with pytest.raises(RuntimeError):
        write_shadow_config(
            production_project=production,
            project=project,
            shadow_root=production / "shadow",
            runtime_root=runtime,
            port=18888,
        )


def test_reconcile_runner_state_terminalizes_completed_stale_pid(tmp_path: Path):
    paths = managed_paths(tmp_path / "validation", "full")
    paths["runner_state"].parent.mkdir(parents=True)
    paths["runner_state"].write_text(json.dumps({"schemaVersion": 1, "pid": 123, "mode": "full"}))
    checkpoint = paths["shadow"] / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"status": "complete", "completedRows": 10, "totalRows": 10}))

    with patch("scripts.qwen_shadow_validation.process_alive", return_value=False):
        state = reconcile_runner_state(paths)

    assert state["alive"] is False
    assert state["status"] == "complete"
    assert "terminalAtEpoch" in state
    assert json.loads(paths["runner_state"].read_text())["status"] == "complete"


def test_reconcile_runner_state_marks_dead_incomplete_runner_interrupted(tmp_path: Path):
    paths = managed_paths(tmp_path / "validation", "full")
    paths["runner_state"].parent.mkdir(parents=True)
    paths["runner_state"].write_text(json.dumps({"schemaVersion": 1, "pid": 123, "mode": "full"}))
    checkpoint = paths["shadow"] / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"status": "running", "completedRows": 4, "totalRows": 10}))

    with patch("scripts.qwen_shadow_validation.process_alive", return_value=False):
        state = reconcile_runner_state(paths)

    assert state["alive"] is False
    assert state["status"] == "interrupted"
