from __future__ import annotations

import plistlib
from pathlib import Path

LAUNCHD_LABEL = "ai.openclaw.qwen-local-embedding"


def build_launchd_plist(
    *, server: Path, model: Path, api_key_file: Path, port: int,
    stdout_path: Path, stderr_path: Path,
) -> bytes:
    if not 1024 <= int(port) <= 65535:
        raise ValueError("launchd sidecar port must be unprivileged")
    argv = [
        str(server), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
        "--embedding", "--pooling", "last", "--ctx-size", "4096", "--batch-size", "4096",
        "--ubatch-size", "2048", "--parallel", "1", "--no-webui", "--api-key-file", str(api_key_file),
    ]
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "ThrottleInterval": 10,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "SoftResourceLimits": {"NumberOfFiles": 4096},
    }, fmt=plistlib.FMT_XML, sort_keys=True)
