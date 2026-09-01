#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.installer.artifacts import LLAMA_CPP, QWEN_MODEL, validate_manifest
from src.installer.downloader import ArtifactDownloader
from src.installer.qwen_installer import DEFAULT_TARGET, QwenInstaller
from src.lifecycle.llama_server_manager import LlamaServerManager


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def manager_for(installer: QwenInstaller, port: int) -> LlamaServerManager:
    return LlamaServerManager(
        server_binary=installer.server_path,
        model_path=installer.model_path,
        api_key_file=installer.api_key_file,
        state_dir=installer.target_dir / "run",
        port=port,
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Manage the Qwen local-only OpenClaw embedding runtime")
    root.add_argument("command", choices=("install", "verify", "start", "stop", "status", "health", "uninstall"))
    root.add_argument("--target", default=str(DEFAULT_TARGET))
    root.add_argument("--artifact-cache", default=str(DEFAULT_TARGET.parent / "qwen-local-artifacts"))
    root.add_argument("--port", type=int, default=18888)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    validate_manifest()
    installer = QwenInstaller(args.target)
    manager = manager_for(installer, args.port)
    try:
        if args.command == "install":
            installer.system_preflight()
            downloader = ArtifactDownloader(args.artifact_cache)
            model = downloader.fetch(QWEN_MODEL)
            runtime = downloader.fetch(LLAMA_CPP)
            installer.install_from_artifacts(model_source=model, runtime_archive=runtime)
            pid = manager.start()
            emit({"ok": True, "command": "install", "state": "installed_healthy", "pid": pid,
                  "provider": "qwen-local", "runtime": "b10625"})
        elif args.command == "verify":
            manifest = installer.verify_installation()
            emit({"ok": True, "command": "verify", "provider": manifest["provider"],
                  "schemaVersion": manifest["schemaVersion"], "runtime": manifest["runtimeRelease"]})
        elif args.command == "start":
            installer.verify_installation()
            emit({"ok": True, "command": "start", "pid": manager.start()})
        elif args.command == "stop":
            manager.stop()
            emit({"ok": True, "command": "stop", "running": False})
        elif args.command == "status":
            installed = installer.manifest_path.is_file()
            state = manager.status() if installed else {"running": False, "healthy": False, "pid": None, "port": args.port}
            emit({"ok": True, "command": "status", "installed": installed, "provider": "qwen-local", **state})
        elif args.command == "health":
            installer.verify_installation()
            state = manager.status()
            canary = bool(state["running"] and manager.embedding_canary())
            emit({"ok": canary, "command": "health", "running": state["running"], "healthy": state["healthy"],
                  "embeddingCanary": canary})
            return 0 if canary else 1
        elif args.command == "uninstall":
            manager.stop_for_uninstall()
            emit({"ok": True, "command": "uninstall", **installer.uninstall()})
        return 0
    except Exception as error:
        emit({"ok": False, "command": args.command, "errorType": type(error).__name__, "message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
