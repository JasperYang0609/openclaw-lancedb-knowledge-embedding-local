#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def evaluate(mode_root: Path, step: int = 10) -> dict:
    checkpoint_path = mode_root / "shadow" / "checkpoint.json"
    runner_path = mode_root / "runner.pid.json"
    report_state_path = mode_root / "progress-report-state.json"
    if not checkpoint_path.is_file():
        return {"event": "error", "reason": "checkpoint_missing"}
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"event": "error", "reason": "checkpoint_invalid"}
    total = int(checkpoint.get("totalRows") or 0)
    completed = int(checkpoint.get("completedRows") or 0)
    if total <= 0 or completed < 0 or completed > total:
        return {"event": "error", "reason": "checkpoint_counts_invalid"}
    percent = completed * 100 / total
    milestone = min(100, int(percent // step) * step)
    previous = None
    if report_state_path.is_file():
        try:
            previous = json.loads(report_state_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"event": "error", "reason": "report_state_invalid"}
    status = checkpoint.get("status")
    runner_alive = False
    if runner_path.is_file():
        try:
            runner_alive = alive(int(json.loads(runner_path.read_text())["pid"]))
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            runner_alive = False
    terminal = "complete" if status == "complete" and completed == total else None
    if status == "running" and not runner_alive:
        terminal = "error"
    current_key = f"{terminal or 'running'}:{milestone}"
    if previous is None and terminal is None:
        atomic_json(report_state_path, {"lastKey": current_key})
        return {"event": "no_change", "initialized": True}
    if previous is not None and previous.get("lastKey") == current_key:
        return {"event": "no_change"}
    atomic_json(report_state_path, {"lastKey": current_key})
    payload = {
        "event": terminal or "progress",
        "completedRows": completed,
        "totalRows": total,
        "percent": round(percent, 1),
        "milestone": milestone,
        "rowsPerSecond": checkpoint.get("rowsPerSecond"),
        "etaSeconds": checkpoint.get("etaSeconds"),
        "runnerAlive": runner_alive,
    }
    if terminal == "error":
        payload["reason"] = "runner_stopped_before_completion"
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode-root", required=True)
    parser.add_argument("--step", type=int, default=10)
    args = parser.parse_args()
    if args.step < 1 or args.step > 100:
        raise ValueError("step must be from 1 through 100")
    print(json.dumps(evaluate(Path(args.mode_root).expanduser().resolve(), args.step), ensure_ascii=False))


if __name__ == "__main__":
    main()
