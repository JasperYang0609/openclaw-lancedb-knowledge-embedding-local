import json
from pathlib import Path
from unittest.mock import patch

from scripts.qwen_progress_gate import evaluate


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_progress_gate_initializes_then_emits_new_milestone(tmp_path: Path):
    write_json(tmp_path / "shadow" / "checkpoint.json", {
        "status": "running", "completedRows": 5, "totalRows": 100,
    })
    write_json(tmp_path / "runner.pid.json", {"pid": 123})
    with patch("scripts.qwen_progress_gate.alive", return_value=True):
        assert evaluate(tmp_path)["event"] == "no_change"
        assert evaluate(tmp_path)["event"] == "no_change"
        write_json(tmp_path / "shadow" / "checkpoint.json", {
            "status": "running", "completedRows": 11, "totalRows": 100,
            "rowsPerSecond": 2.0, "etaSeconds": 44.5,
        })
        event = evaluate(tmp_path)
    assert event["event"] == "progress"
    assert event["milestone"] == 10


def test_progress_gate_reports_stopped_runner_and_completion(tmp_path: Path):
    write_json(tmp_path / "shadow" / "checkpoint.json", {
        "status": "running", "completedRows": 40, "totalRows": 100,
    })
    write_json(tmp_path / "runner.pid.json", {"pid": 123})
    with patch("scripts.qwen_progress_gate.alive", return_value=False):
        assert evaluate(tmp_path)["event"] == "error"
        write_json(tmp_path / "shadow" / "checkpoint.json", {
            "status": "complete", "completedRows": 100, "totalRows": 100,
        })
        event = evaluate(tmp_path)
    assert event["event"] == "complete"
