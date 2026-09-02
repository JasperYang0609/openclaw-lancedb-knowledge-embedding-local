#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.installer.artifacts import LLAMA_CPP, QWEN_MODEL, validate_manifest
from src.installer.downloader import ArtifactDownloader
from src.installer.qwen_installer import DEFAULT_TARGET, QwenInstaller
from src.lifecycle.llama_server_manager import LlamaServerManager
from src.openclaw_integration.core import IntegrationManager, IntegrationPaths, OpenClawCli
from src.openclaw_integration.launchd import LAUNCHD_LABEL


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
    root.add_argument("command", choices=(
        "install", "verify", "start", "stop", "status", "health", "uninstall",
        "integrate-openclaw", "verify-openclaw", "rollback-openclaw", "uninstall-openclaw",
    ))
    root.add_argument("--target", default=str(DEFAULT_TARGET))
    root.add_argument("--artifact-cache", default=str(DEFAULT_TARGET.parent / "qwen-local-artifacts"))
    root.add_argument("--port", type=int)
    root.add_argument("--workspace", default=str(Path.home() / ".openclaw/workspace"))
    root.add_argument("--integration-state", default=str(Path.home() / "Library/Application Support/OpenClaw/qwen-local-integration"))
    root.add_argument("--openclaw", default="")
    root.add_argument("--node", default="")
    root.add_argument("--profile", default="")
    root.add_argument("--agent", default="main")
    return root


def integration_manager(args: argparse.Namespace) -> IntegrationManager:
    home = Path.home().resolve()
    workspace = Path(args.workspace).expanduser().resolve()
    paths = IntegrationPaths(
        home=home,
        workspace=workspace,
        project_root=workspace / "knowledge-lancedb-qwen-local",
        runtime_root=Path(args.target).expanduser().resolve(),
        state_root=Path(args.integration_state).expanduser().resolve(),
        launchd_plist=home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
    )
    openclaw = args.openclaw or shutil.which("openclaw")
    node = args.node or shutil.which("node")
    if not openclaw or not node:
        raise RuntimeError("OpenClaw and Node.js executables are required for integration")
    return IntegrationManager(
        paths=paths, repo_root=ROOT, cli=OpenClawCli(openclaw, profile=args.profile or None),
        node_path=Path(node), agent=args.agent,
    )


def integration_is_committed(args: argparse.Namespace) -> bool:
    manifest = Path(args.integration_state).expanduser() / "transaction.json"
    if manifest.is_symlink() or not manifest.is_file():
        return False
    try:
        return json.loads(manifest.read_text()).get("phase") == "committed"
    except (OSError, json.JSONDecodeError):
        return False


def resolve_port(installer: QwenInstaller, requested_port: int | None) -> int:
    if installer.manifest_path.exists():
        installed_port = int(installer.verify_installation()["runtimePort"])
        if requested_port is not None and requested_port != installed_port:
            raise RuntimeError(
                f"Requested port {requested_port} differs from installed port {installed_port}; reinstall to change it"
            )
        return installed_port
    return 18888 if requested_port is None else requested_port


def integrate_with_runtime_handoff(
        *, runtime_manager: LlamaServerManager, integration: IntegrationManager,
        runtime_manifest: dict,
) -> dict:
    runtime_was_running = bool(runtime_manager.status().get("running"))
    if runtime_was_running:
        runtime_manager.stop()
    try:
        return integration.integrate(runtime_manifest)
    except Exception:
        if runtime_was_running:
            runtime_manager.start()
        raise


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    validate_manifest()
    installer = QwenInstaller(args.target)
    try:
        port = resolve_port(installer, args.port)
        manager = manager_for(installer, port)
        if args.command == "install":
            installer.system_preflight()
            downloader = ArtifactDownloader(args.artifact_cache)
            model = downloader.fetch(QWEN_MODEL)
            runtime = downloader.fetch(LLAMA_CPP)
            installer.install_from_artifacts(model_source=model, runtime_archive=runtime, runtime_port=port)
            pid = manager.start()
            emit({"ok": True, "command": "install", "state": "installed_healthy", "pid": pid,
                  "provider": "qwen-local", "runtime": "b10625"})
        elif args.command == "verify":
            manifest = installer.verify_installation()
            emit({"ok": True, "command": "verify", "provider": manifest["provider"],
                  "schemaVersion": manifest["schemaVersion"], "runtime": manifest["runtimeRelease"]})
        elif args.command == "start":
            installer.verify_installation()
            if integration_is_committed(args):
                integration_manager(args).activate_launchd()
                emit({"ok": True, "command": "start", "service": "launchd"})
            else:
                emit({"ok": True, "command": "start", "pid": manager.start()})
        elif args.command == "stop":
            if integration_is_committed(args):
                integration_manager(args).deactivate_launchd()
            else:
                manager.stop()
            emit({"ok": True, "command": "stop", "running": False})
        elif args.command == "status":
            installed = installer.manifest_path.is_file()
            if installed and integration_is_committed(args):
                healthy = manager.is_healthy()
                state = {"running": healthy, "healthy": healthy, "pid": None, "port": port,
                         "endpoint": f"http://127.0.0.1:{port}", "service": "launchd"}
            else:
                state = manager.status() if installed else {"running": False, "healthy": False, "pid": None, "port": port}
            emit({"ok": True, "command": "status", "installed": installed, "provider": "qwen-local", **state})
        elif args.command == "health":
            installer.verify_installation()
            state = manager.status()
            running = manager.is_healthy() if integration_is_committed(args) else state["running"]
            canary = bool(running and manager.embedding_canary())
            emit({"ok": canary, "command": "health", "running": running, "healthy": running,
                  "embeddingCanary": canary})
            return 0 if canary else 1
        elif args.command == "uninstall":
            manager.stop_for_uninstall()
            emit({"ok": True, "command": "uninstall", **installer.uninstall()})
        elif args.command == "integrate-openclaw":
            if not installer.manifest_path.exists():
                installer.system_preflight()
                downloader = ArtifactDownloader(args.artifact_cache)
                model = downloader.fetch(QWEN_MODEL)
                runtime = downloader.fetch(LLAMA_CPP)
                installer.install_from_artifacts(model_source=model, runtime_archive=runtime, runtime_port=port)
            runtime_manifest = installer.verify_installation()
            result = integrate_with_runtime_handoff(
                runtime_manager=manager,
                integration=integration_manager(args),
                runtime_manifest=runtime_manifest,
            )
            emit({"ok": True, "command": "integrate-openclaw", **result})
        elif args.command == "verify-openclaw":
            result = integration_manager(args).verify()
            emit({"ok": True, "command": "verify-openclaw", **result})
        elif args.command == "rollback-openclaw":
            result = integration_manager(args).rollback()
            emit({"ok": True, "command": "rollback-openclaw", **result})
        elif args.command == "uninstall-openclaw":
            result = integration_manager(args).uninstall()
            emit({"ok": True, "command": "uninstall-openclaw", **result})
        return 0
    except Exception as error:
        emit({"ok": False, "command": args.command, "errorType": type(error).__name__, "message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
