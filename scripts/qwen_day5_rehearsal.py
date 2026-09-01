#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.qwen_shadow_validation import atomic_json, resolve_specific  # noqa: E402
from src.installer.qwen_installer import QwenInstaller, sha256_file  # noqa: E402
from src.lifecycle.llama_server_manager import LlamaServerManager  # noqa: E402


def manager_for(installer: QwenInstaller, port: int) -> LlamaServerManager:
    return LlamaServerManager(
        server_binary=installer.server_path,
        model_path=installer.model_path,
        api_key_file=installer.api_key_file,
        state_dir=installer.target_dir / "run",
        port=port,
    )


def install_start_stop_uninstall(
    *,
    installer: QwenInstaller,
    model: Path,
    server: Path,
    port: int,
) -> dict:
    manifest = installer.install_from_verified_sources(
        model_source=model,
        server_source=server,
        development_model_link=True,
    )
    verified = installer.verify_installation()
    manager = manager_for(installer, port)
    try:
        manager.start(timeout_seconds=180)
        healthy = manager.is_healthy() and manager.embedding_canary()
        if not healthy:
            raise RuntimeError("Installed sidecar failed the embedding canary")
    finally:
        manager.stop(timeout_seconds=30)
    uninstalled = installer.uninstall()
    if installer.target_dir.exists():
        raise RuntimeError("Managed runtime target remained after uninstall")
    return {
        "installVerified": manifest == verified,
        "healthCanary": healthy,
        "loopbackPort": port,
        "uninstallStatus": uninstalled["status"],
        "managedTargetRemoved": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Qwen Day 5 fresh install and restore rehearsal")
    parser.add_argument("--root", required=True)
    parser.add_argument("--model-source", required=True)
    parser.add_argument("--server-source", required=True)
    parser.add_argument("--port", type=int, default=18890)
    args = parser.parse_args()

    root = resolve_specific(args.root)
    model = Path(args.model_source).expanduser().resolve()
    server = Path(args.server_source).expanduser().resolve()
    if root.exists():
        raise RuntimeError("Day 5 rehearsal root must not already exist")
    if not model.is_file() or not server.is_file():
        raise FileNotFoundError("Verified model and runtime sources are required")
    if not 1024 <= args.port <= 65535:
        raise ValueError("Port must be from 1024 through 65535")

    root.mkdir(parents=True)
    evidence = {
        "schemaVersion": 1,
        "status": "running",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "isolation": {"freshSpecificRoot": True, "productionTouched": False},
        "sourceArtifacts": {
            "modelSha256": sha256_file(model),
            "runtimeSha256": sha256_file(server),
        },
    }
    model_before = evidence["sourceArtifacts"]["modelSha256"]
    server_before = evidence["sourceArtifacts"]["runtimeSha256"]
    installer = QwenInstaller(root / "runtime")
    try:
        evidence["preflight"] = QwenInstaller.system_preflight()
        evidence["freshInstall"] = install_start_stop_uninstall(
            installer=installer,
            model=model,
            server=server,
            port=args.port,
        )
        evidence["restoreReinstall"] = install_start_stop_uninstall(
            installer=installer,
            model=model,
            server=server,
            port=args.port,
        )
        evidence["sourceArtifactsPreserved"] = (
            sha256_file(model) == model_before and sha256_file(server) == server_before
        )
        if not evidence["sourceArtifactsPreserved"]:
            raise RuntimeError("Source artifact changed during the rehearsal")
        evidence["status"] = "pass"
    except Exception as error:
        evidence["status"] = "fail"
        evidence["errorType"] = type(error).__name__
        evidence["error"] = str(error)[:1000]
        raise
    finally:
        evidence["completedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        evidence["isolation"]["productionTouched"] = False
        if "preflight" in evidence:
            evidence["preflight"].pop("freeBytes", None)
            evidence["preflight"].pop("ramBytes", None)
        atomic_json(root / "reports" / "day5-rehearsal.json", evidence)
        print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
