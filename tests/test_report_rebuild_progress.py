from __future__ import annotations

import json
from pathlib import Path

from scripts import report_rebuild_progress as progress


def test_parse_status_uses_latest_progress_and_exit() -> None:
    status = progress.parse_status(
        "[embedding] local embedded 40/100 (+4)\n"
        "[embedding] local embedded 84/100 (+4)\n"
        "QWEN_REBUILD_EXIT=0\n"
    )
    assert status == {"completed": 84, "total": 100, "exitCode": 0}


def test_state_round_trip_is_private_and_atomic(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    progress.write_state(target, {"milestone": 20})
    assert progress.read_state(target) == {"milestone": 20}
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text()) == {"milestone": 20}


def test_missing_or_invalid_state_is_empty(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    assert progress.read_state(target) == {}
    target.write_text("not-json")
    assert progress.read_state(target) == {}
