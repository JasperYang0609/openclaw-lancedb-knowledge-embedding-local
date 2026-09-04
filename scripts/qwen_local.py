#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
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
from src.openclaw_integration.core import (
    ApprovedDisabledCronCollision,
    IntegrationManager,
    IntegrationPaths,
    IntegrationRollbackIncomplete,
    OpenClawCli,
)
from src.openclaw_integration.launchd import LAUNCHD_LABEL


CURRENT_OWNERSHIP_SCHEMA = "qwen-local-openclaw.v3"
LEGACY_OWNERSHIP_SCHEMAS = frozenset({"qwen-local-openclaw.v2"})
SUPPORTED_OWNERSHIP_SCHEMAS = frozenset({CURRENT_OWNERSHIP_SCHEMA}) | LEGACY_OWNERSHIP_SCHEMAS


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


class RuntimeHandoffRecoveryError(RuntimeError):
    """The primary integration failure survived, but manual-runtime recovery failed."""

    def __init__(self, original_error: Exception, restart_error: Exception) -> None:
        super().__init__(
            "Integration failed after verified rollback, and the prior manual runtime could not be restarted"
        )
        self.original_error = original_error
        self.restart_error = restart_error
        self.recovery_state = "manual_runtime_restart_failed"


def recovery_error_fields(error: Exception) -> dict[str, str]:
    if isinstance(error, IntegrationRollbackIncomplete):
        return {
            "recoveryState": error.recovery_state,
            "primaryErrorType": type(error.original_error).__name__,
            "rollbackErrorType": type(error.rollback_error).__name__,
            "manualRuntimeRestart": "not_attempted",
        }
    if isinstance(error, RuntimeHandoffRecoveryError):
        return {
            "recoveryState": error.recovery_state,
            "primaryErrorType": type(error.original_error).__name__,
            "restartErrorType": type(error.restart_error).__name__,
            "manualRuntimeRestart": "failed",
        }
    return {}


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
    root.add_argument("--snapshot-root", default="")
    root.add_argument("--report-channel", default="")
    root.add_argument("--report-to", default="")
    root.add_argument("--report-account-id", default="default")
    root.add_argument("--timezone", default="Asia/Taipei")
    root.add_argument("--legacy-snapshot-job-id", default="")
    root.add_argument("--legacy-snapshot-job-sha256", default="")
    root.add_argument(
        "--approve-disabled-collision-job-id", default="",
        help=(
            "Exact disabled legacy cron job ID printed by the fail-closed collision "
            "diagnostic; requires the SHA-256 and role options"
        ),
    )
    root.add_argument(
        "--approve-disabled-collision-job-sha256", default="",
        help=(
            "ID-inclusive SHA-256 of the complete normalized cron contract printed by "
            "the current-version collision diagnostic"
        ),
    )
    root.add_argument(
        "--approve-disabled-collision-role", choices=("incremental",), default="",
        help="Exact approved collision role; only incremental is supported",
    )
    return root


def load_stored_transaction(state_root: Path) -> dict | None:
    manifest = state_root / "transaction.json"
    if manifest.exists() or manifest.is_symlink():
        if manifest.is_symlink() or not manifest.is_file():
            raise RuntimeError("Stored integration ownership receipt is unsafe")
        metadata = manifest.stat()
        if metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_mode & 0o077:
            raise RuntimeError("Stored integration ownership receipt permissions are unsafe")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Stored integration ownership receipt is malformed") from error
        ownership = payload.get("ownership") if isinstance(payload, dict) else None
        if isinstance(ownership, dict) and ownership.get("schema") in SUPPORTED_OWNERSHIP_SCHEMAS:
            return payload
    return None


def load_stored_ownership(state_root: Path) -> dict | None:
    transaction = load_stored_transaction(state_root)
    return transaction.get("ownership") if transaction is not None else None


def load_committed_stored_ownership(state_root: Path) -> dict | None:
    transaction = load_stored_transaction(state_root)
    if transaction is None \
            or type(transaction.get("schemaVersion")) is not int \
            or transaction.get("schemaVersion") != 1 \
            or transaction.get("phase") != "committed":
        return None
    return transaction["ownership"]


def resolve_report_target(args: argparse.Namespace, state_root: Path) -> tuple[str, str | None, str]:
    report_channel = str(getattr(args, "report_channel", "") or "")
    report_to = str(getattr(args, "report_to", "") or "")
    report_account_id = str(getattr(args, "report_account_id", "default") or "default")
    if bool(report_channel) != bool(report_to):
        raise RuntimeError("Failure alerts require both --report-channel and --report-to")
    if report_channel and report_to:
        return report_channel, report_to, report_account_id

    ownership = load_stored_ownership(state_root)
    if ownership is not None:
        stored_channel = ownership.get("reportChannel")
        stored_to = ownership.get("reportTo")
        stored_account = ownership.get("reportAccountId", "default")
        if isinstance(stored_channel, str) and stored_channel \
                and isinstance(stored_to, str) and stored_to \
                and isinstance(stored_account, str) and stored_account:
            return stored_channel, stored_to, stored_account

    if getattr(args, "command", "integrate-openclaw") in {
        "rollback-openclaw", "uninstall-openclaw", "start", "stop",
    }:
        return "last", None, report_account_id
    raise RuntimeError(
        "Explicit --report-channel and --report-to are required for fresh integration or verification"
    )


def resolve_snapshot_root(args: argparse.Namespace, state_root: Path) -> Path | None:
    explicit = str(getattr(args, "snapshot_root", "") or "")
    ownership = load_stored_ownership(state_root)
    stored_root: Path | None = None
    if ownership is not None:
        if "snapshotRoot" not in ownership:
            raise RuntimeError("Stored snapshot root is missing")
        stored = ownership["snapshotRoot"]
        if not isinstance(stored, str) or not stored:
            raise RuntimeError("Stored snapshot root is malformed")
        candidate = Path(stored).expanduser()
        if not candidate.is_absolute():
            raise RuntimeError("Stored snapshot root must be absolute")
        stored_root = Path(os.path.abspath(candidate))

    if explicit:
        explicit_root = Path(os.path.abspath(Path(explicit).expanduser()))
        recovery_commands = {
            "rollback-openclaw", "uninstall-openclaw", "verify-openclaw",
        }
        if getattr(args, "command", "") in recovery_commands \
                and stored_root is not None and explicit_root != stored_root:
            raise RuntimeError(
                "Explicit snapshot root does not match stored integration ownership"
            )
        return explicit_root
    return stored_root


def resolve_disabled_collision_approval(
    args: argparse.Namespace, state_root: Path,
) -> ApprovedDisabledCronCollision | None:
    if getattr(args, "command", "") not in {"integrate-openclaw", "verify-openclaw"}:
        return None
    job_id = str(getattr(args, "approve_disabled_collision_job_id", "") or "")
    contract_sha256 = str(
        getattr(args, "approve_disabled_collision_job_sha256", "") or ""
    )
    role = str(getattr(args, "approve_disabled_collision_role", "") or "")
    values = (job_id, contract_sha256, role)
    if any(values):
        if not all(values):
            raise RuntimeError(
                "Disabled collision approval requires job id, SHA-256, and role together"
            )
        return ApprovedDisabledCronCollision(
            job_id=job_id, contract_sha256=contract_sha256, role=role,
        )

    ownership = load_committed_stored_ownership(state_root)
    if ownership is None or "approvedDisabledCollision" not in ownership:
        return None
    stored = ownership["approvedDisabledCollision"]
    if not isinstance(stored, dict) or set(stored) != {"jobId", "contractSha256", "role"} \
            or any(not isinstance(stored.get(key), str) for key in stored):
        raise RuntimeError("Stored disabled collision approval receipt is malformed")
    try:
        return ApprovedDisabledCronCollision(
            job_id=stored["jobId"],
            contract_sha256=stored["contractSha256"],
            role=stored["role"],
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("Stored disabled collision approval receipt is malformed") from error


def integration_manager(args: argparse.Namespace) -> IntegrationManager:
    home = Path.home().resolve()
    workspace = Path(os.path.abspath(Path(args.workspace).expanduser()))
    state_root = Path(os.path.abspath(Path(args.integration_state).expanduser()))
    paths = IntegrationPaths(
        home=home,
        workspace=workspace,
        project_root=workspace / "knowledge-lancedb-qwen-local",
        runtime_root=Path(os.path.abspath(Path(args.target).expanduser())),
        state_root=state_root,
        launchd_plist=home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
    )
    openclaw = args.openclaw or shutil.which("openclaw")
    node = args.node or shutil.which("node")
    if not openclaw or not node:
        raise RuntimeError("OpenClaw and Node.js executables are required for integration")
    report_channel, report_to, report_account_id = resolve_report_target(args, state_root)
    approved_collision = resolve_disabled_collision_approval(args, state_root)
    snapshot_root = resolve_snapshot_root(args, state_root)
    return IntegrationManager(
        paths=paths, repo_root=ROOT, cli=OpenClawCli(openclaw, profile=args.profile or None),
        node_path=Path(node), agent=args.agent,
        snapshot_root=snapshot_root,
        report_channel=report_channel,
        report_to=report_to,
        report_account_id=report_account_id,
        timezone_name=getattr(args, "timezone", "Asia/Taipei"),
        legacy_snapshot_job_id=getattr(args, "legacy_snapshot_job_id", "") or None,
        legacy_snapshot_job_sha256=getattr(args, "legacy_snapshot_job_sha256", "") or None,
        approved_disabled_collision=approved_collision,
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
    except IntegrationRollbackIncomplete:
        raise
    except Exception as original_error:
        if runtime_was_running:
            try:
                runtime_manager.start()
            except Exception as restart_error:
                raise RuntimeHandoffRecoveryError(original_error, restart_error) from original_error
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
        emit({
            "ok": False,
            "command": args.command,
            "errorType": type(error).__name__,
            "message": str(error),
            **recovery_error_fields(error),
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
