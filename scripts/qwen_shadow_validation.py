#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "openclaw-lancedb-knowledge" / "assets" / "knowledge-lancedb-template"
sys.path.insert(0, str(REPO_ROOT))

from src.installer.qwen_installer import (  # noqa: E402
    LLAMA_CPP_REVISION,
    LLAMA_SERVER_SHA256,
    QWEN_Q5_SHA256,
    QwenInstaller,
)
from src.lifecycle.llama_server_manager import LlamaServerManager  # noqa: E402


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def resolve_specific(path_value: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if path == Path(path.anchor) or len(path.parts) < 5:
        raise ValueError("Validation root must be a specific non-root directory")
    return path


def is_inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def managed_paths(root: Path, mode: str) -> dict[str, Path]:
    mode_root = root / mode
    runtime_root = root / "runtime"
    return {
        "root": root,
        "mode_root": mode_root,
        "project": mode_root / "project",
        "shadow": mode_root / "shadow",
        "runtime": runtime_root,
        "runner_state": mode_root / "runner.pid.json",
        "runner_log": mode_root / "runner.log",
    }


def manager_for(root: Path, port: int) -> LlamaServerManager:
    installer = QwenInstaller(root / "runtime")
    installer.verify_installation()
    return LlamaServerManager(
        server_binary=installer.server_path,
        model_path=installer.model_path,
        api_key_file=installer.api_key_file,
        state_dir=installer.target_dir / "run",
        port=port,
    )


def write_shadow_config(
    *,
    production_project: Path,
    project: Path,
    shadow_root: Path,
    runtime_root: Path,
    port: int,
) -> None:
    source_config = production_project / "config" / "source-map.json"
    production_db = (production_project / "data" / "lancedb").resolve()
    production_cache = (production_project / "data" / "embedding-cache").resolve()
    if not source_config.is_file():
        raise FileNotFoundError("Production source-map.json is missing")
    if is_inside(production_project, shadow_root) or is_inside(shadow_root, production_project):
        raise RuntimeError("Shadow validation root overlaps the Production project")
    config = json.loads(source_config.read_text())
    config["dbPath"] = str((shadow_root / "data" / "lancedb").resolve())
    config["tableName"] = "knowledge_chunks_qwen_shadow"
    config["embedding"] = {
        "provider": "qwen-local",
        "model": "Qwen3-Embedding-4B-Q5_K_M",
        "profile": "custom",
        "dimensions": 768,
        "nativeDimensions": 2560,
        "quantization": "Q5_K_M",
        "modelSha256": QWEN_Q5_SHA256,
        "runtimeRevision": LLAMA_CPP_REVISION,
        "runtimeSha256": LLAMA_SERVER_SHA256,
        "pooling": "last",
        "queryInstruction": "Given a web search query, retrieve relevant passages that answer the query",
        "normalization": "truncate-then-l2",
        "endpoint": f"http://127.0.0.1:{port}",
        "apiKeyFile": str((runtime_root / "run" / "api-key").resolve()),
        "batchSize": 4,
        "maxInputChars": 12000,
        "timeoutMs": 120000,
        "maxRetries": 3,
    }
    config["privacy"] = {
        "discordRawApproval": "LOCAL_ONLY",
        "cloudFallback": "DISABLED",
    }
    config["shadow"] = {
        "enabled": True,
        "root": str(shadow_root.resolve()),
        "forbiddenPaths": [
            str(production_project.resolve()),
            str(production_db),
            str(production_cache),
            str(source_config.resolve()),
        ],
        "indexBatchSize": 32,
    }
    config_path = project / "config" / "source-map.json"
    atomic_json(config_path, config)


def prepare(args: argparse.Namespace) -> None:
    root = resolve_specific(args.root)
    production_project = Path(args.production_project).expanduser().resolve()
    paths = managed_paths(root, args.mode)
    if paths["mode_root"].exists() and not args.resume:
        raise RuntimeError("Mode root already exists; pass --resume only to preserve a known checkpoint")
    QwenInstaller.system_preflight()
    installer = QwenInstaller(paths["runtime"])
    installer.install_from_verified_sources(
        model_source=args.model_source,
        server_source=args.server_source,
        development_model_link=True,
    )
    if not paths["project"].exists():
        shutil.copytree(
            TEMPLATE_ROOT,
            paths["project"],
            ignore=shutil.ignore_patterns("node_modules", "data", "reports", ".DS_Store"),
        )
    write_shadow_config(
        production_project=production_project,
        project=paths["project"],
        shadow_root=paths["shadow"],
        runtime_root=paths["runtime"],
        port=args.port,
    )
    subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=paths["project"], check=True)
    atomic_json(paths["mode_root"] / "prepare-manifest.json", {
        "schemaVersion": 1,
        "mode": args.mode,
        "preparedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "project": str(paths["project"]),
        "shadowRoot": str(paths["shadow"]),
        "productionProject": str(production_project),
    })
    print(json.dumps({"status": "prepared", "mode": args.mode}, ensure_ascii=False))


def start(args: argparse.Namespace) -> None:
    root = resolve_specific(args.root)
    pid = manager_for(root, args.port).start(timeout_seconds=args.timeout)
    print(json.dumps({"status": "healthy", "pid": pid, "port": args.port}))


def stop(args: argparse.Namespace) -> None:
    root = resolve_specific(args.root)
    manager_for(root, args.port).stop()
    print(json.dumps({"status": "stopped", "port": args.port}))


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def shadow_runner_alive(pid: int) -> bool:
    if not process_alive(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return "shadow-index.js" in result.stdout and "node" in result.stdout


def reconcile_runner_state(paths: dict[str, Path]) -> dict | None:
    """Convert a dead runner PID record into an explicit terminal state."""
    runner_path = paths["runner_state"]
    if not runner_path.is_file():
        return None
    runner = json.loads(runner_path.read_text())
    runner_alive = shadow_runner_alive(int(runner["pid"]))
    runner["alive"] = runner_alive
    if runner_alive:
        runner["status"] = "running"
        return runner
    checkpoint_path = paths["shadow"] / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.is_file() else None
    complete = bool(
        checkpoint
        and checkpoint.get("status") == "complete"
        and int(checkpoint.get("completedRows") or 0) == int(checkpoint.get("totalRows") or -1)
    )
    runner["status"] = "complete" if complete else "interrupted"
    runner["terminalAtEpoch"] = runner.get("terminalAtEpoch") or time.time()
    atomic_json(runner_path, runner)
    return runner


def run_index(args: argparse.Namespace, *, background: bool) -> None:
    root = resolve_specific(args.root)
    paths = managed_paths(root, args.mode)
    if not paths["project"].is_dir():
        raise RuntimeError("Validation project is not prepared")
    command = ["node", "src/shadow-index.js", "--index-batch-size", str(args.batch_size)]
    if args.limit:
        command.extend(["--limit", str(args.limit)])
    if not background:
        subprocess.run(command, cwd=paths["project"], check=True)
        return
    if paths["runner_state"].is_file():
        previous = json.loads(paths["runner_state"].read_text())
        if process_alive(int(previous["pid"])):
            raise RuntimeError("Shadow runner is already active")
    paths["runner_log"].parent.mkdir(parents=True, exist_ok=True)
    with paths["runner_log"].open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=paths["project"],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            shell=False,
            start_new_session=True,
        )
    atomic_json(paths["runner_state"], {
        "schemaVersion": 1,
        "pid": process.pid,
        "mode": args.mode,
        "startedAtEpoch": time.time(),
        "log": str(paths["runner_log"]),
    })
    time.sleep(1)
    if process.poll() is not None:
        raise RuntimeError(f"Shadow runner exited immediately with code {process.returncode}")
    print(json.dumps({"status": "running", "pid": process.pid, "mode": args.mode}))


def status(args: argparse.Namespace) -> None:
    root = resolve_specific(args.root)
    paths = managed_paths(root, args.mode)
    sidecar = manager_for(root, args.port)
    runner = reconcile_runner_state(paths)
    checkpoint = None
    checkpoint_path = paths["shadow"] / "checkpoint.json"
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text())
    print(json.dumps({
        "sidecarHealthy": sidecar.is_healthy() and sidecar.embedding_canary(),
        "runner": runner,
        "checkpoint": checkpoint,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the isolated Qwen full-shadow validation")
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, default=18888)
    subparsers = parser.add_subparsers(dest="action", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--mode", choices=("canary", "full"), required=True)
    prepare_parser.add_argument("--production-project", required=True)
    prepare_parser.add_argument("--model-source", required=True)
    prepare_parser.add_argument("--server-source", required=True)
    prepare_parser.add_argument("--resume", action="store_true")
    prepare_parser.set_defaults(func=prepare)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--timeout", type=int, default=180)
    start_parser.set_defaults(func=start)

    stop_parser = subparsers.add_parser("stop")
    stop_parser.set_defaults(func=stop)

    for name, is_background in (("run", False), ("run-background", True)):
        run_parser = subparsers.add_parser(name)
        run_parser.add_argument("--mode", choices=("canary", "full"), required=True)
        run_parser.add_argument("--limit", type=int, default=0)
        run_parser.add_argument("--batch-size", type=int, default=32)
        run_parser.set_defaults(func=lambda ns, bg=is_background: run_index(ns, background=bg))

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--mode", choices=("canary", "full"), required=True)
    status_parser.set_defaults(func=status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
