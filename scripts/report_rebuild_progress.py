#!/usr/bin/env python3
"""Emit sparse, safe progress updates for a long-running Qwen rebuild."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


PROGRESS_RE = re.compile(r"\[embedding\] local embedded (\d+)/(\d+)")
EXIT_RE = re.compile(r"QWEN_REBUILD_EXIT=(\d+)")


def parse_status(text: str) -> dict[str, int | str | None]:
    progress = PROGRESS_RE.findall(text)
    exits = EXIT_RE.findall(text)
    completed = int(progress[-1][0]) if progress else 0
    total = int(progress[-1][1]) if progress else 0
    exit_code = int(exits[-1]) if exits else None
    return {"completed": completed, "total": total, "exitCode": exit_code}


def read_state(path: Path) -> dict:
    if path.is_symlink() or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(path: Path, payload: dict) -> None:
    if path.is_symlink():
        raise RuntimeError("progress state path must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def disable_cron(cron_id: str) -> None:
    if not cron_id:
        return
    subprocess.run(
        ["openclaw", "cron", "disable", cron_id],
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--cron-id", default="")
    args = parser.parse_args()

    log_path = Path(args.log).expanduser().resolve()
    state_path = Path(args.state).expanduser().resolve()
    if log_path.is_symlink() or not log_path.is_file():
        return 0

    status = parse_status(log_path.read_text(encoding="utf-8", errors="replace"))
    completed = int(status["completed"])
    total = int(status["total"])
    exit_code = status["exitCode"]
    milestone = min(100, int((completed * 100) / total) // 10 * 10) if total else 0
    previous = read_state(state_path)

    if exit_code is not None:
        terminal_key = f"exit-{exit_code}"
        if previous.get("terminal") != terminal_key:
            write_state(state_path, {**status, "milestone": milestone, "terminal": terminal_key})
            if exit_code == 0:
                print(f"Qwen 全量索引已完成 {completed:,}/{total:,}；接著執行 OpenClaw 地端整合與驗收。")
            else:
                print(f"Qwen 全量索引重建失敗（exit {exit_code}）；既有地端索引維持服務，未切回 Gemini。")
        disable_cron(args.cron_id)
        return 0 if exit_code == 0 else 1

    if milestone >= 10 and milestone > int(previous.get("milestone", 0)):
        write_state(state_path, {**status, "milestone": milestone})
        print(f"Qwen 全量索引進度：{completed:,}/{total:,}（{completed / total:.1%}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
