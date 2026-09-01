from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_status_is_redacted_for_uninstalled_target(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, str(ROOT / "scripts/qwen_local.py"), "status", "--target",
                             str(tmp_path / "managed/qwen")], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["installed"] is False
    assert payload["provider"] == "qwen-local"
    assert "api" not in result.stdout.lower()
    assert str(tmp_path) not in result.stdout


def test_unsupported_command_is_rejected_without_shell(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, str(ROOT / "scripts/qwen_local.py"), "install;echo-pwned",
                             "--target", str(tmp_path / "managed/qwen")], capture_output=True, text=True)
    assert result.returncode != 0
    assert "pwned\n" not in result.stdout
