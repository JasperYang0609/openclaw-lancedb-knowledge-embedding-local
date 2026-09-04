#!/usr/bin/env python3
"""Run the installer-owned immutable Qwen snapshot and verification contract."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from backup_health_component import build_receipt, load_json, write_receipt
from index_lock import acquire as acquire_index_lock
from index_lock import release as release_index_lock
from snapshot_knowledge_assets import (
    create_snapshot,
    prune_daily_snapshots,
    prune_transient_snapshots,
    restore_canary,
    verify_database,
    verify_snapshot,
)


OWNERSHIP_SCHEMA = "qwen-local-openclaw.v2"
SNAPSHOT_CONTRACT = "qwen-local-verified-snapshot.v1"
DEFAULT_LOCK_WAIT_SECONDS = 30 * 60
DEFAULT_LOCK_POLL_SECONDS = 15
TABLE_NAME = "knowledge_chunks_qwen_local_768"
SNAPSHOT_RUN_LOCK = ".snapshot-run.lock"
HEALTH_RECEIPT_SCHEMA = "backup-health-component.v1"
INCREMENTAL_DECLARATION_KEY = "openclaw-lancedb-knowledge-local-incremental-v1"
SNAPSHOT_DECLARATION_KEY = "openclaw-lancedb-knowledge-local-snapshot-v1"
INITIAL_DECLARATION_KEY = "openclaw-lancedb-knowledge-local-initial-v1"


class SnapshotBusy(RuntimeError):
    """A verified snapshot is already active; callers should safely skip."""


def _inside(path: Path, parent: Path) -> bool:
    resolved = path.resolve(strict=False)
    base = parent.resolve(strict=False)
    return resolved != base and base in resolved.parents


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise RuntimeError("Managed Qwen snapshot paths must not contain symbolic links")


def _validate_owned_directory(path: Path, *, create: bool = False, restricted: bool = False) -> Path:
    _reject_symlink_components(path)
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    if not path.is_dir():
        raise RuntimeError("Managed Qwen snapshot directory is missing or unsafe")
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RuntimeError("Managed Qwen snapshot directory ownership or permissions are unsafe")
    forbidden = 0o077 if restricted else (stat.S_IWGRP | stat.S_IWOTH)
    if metadata.st_mode & forbidden:
        raise RuntimeError("Managed Qwen snapshot directory ownership or permissions are unsafe")
    return path.resolve()


@contextmanager
def snapshot_run_lock(snapshot_root: Path):
    """Serialize snapshot writers without deleting or breaking a live lock."""
    root = _validate_owned_directory(snapshot_root, create=True, restricted=True)
    lock_path = root / SNAPSHOT_RUN_LOCK
    if lock_path.is_symlink():
        raise RuntimeError("Snapshot run lock path is unsafe")
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.getuid():
            raise RuntimeError("Snapshot run lock ownership is unsafe")
        if metadata.st_mode & 0o077:
            raise RuntimeError("Snapshot run lock permissions are too broad")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as error:
            raise SnapshotBusy("Another verified snapshot run is active") from error
        yield
    finally:
        if descriptor is not None:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def load_ownership(manifest_path: Path, *, allow_activation_pending: bool = False) -> dict[str, Any]:
    if not manifest_path.is_absolute():
        raise RuntimeError("Ownership manifest must use an absolute path")
    manifest = load_json(manifest_path, private=True, private_parent=True)
    allowed_phases = {"committed", "activation_pending"} if allow_activation_pending else {"committed"}
    if manifest.get("schemaVersion") != 1 or manifest.get("phase") not in allowed_phases:
        raise RuntimeError("Ownership transaction is not committed")
    ownership = manifest.get("ownership")
    if not isinstance(ownership, dict) or ownership.get("schema") != OWNERSHIP_SCHEMA:
        raise RuntimeError("Ownership contract is missing or unsupported")
    if ownership.get("snapshotContract") != SNAPSHOT_CONTRACT:
        raise RuntimeError("Snapshot contract identity is unsupported")
    if ownership.get("provider") != "qwen-local" or ownership.get("localOnly") is not True:
        raise RuntimeError("Qwen snapshot ownership is not local-only")
    required_strings = (
        "projectRoot", "snapshotRoot", "healthReceiptPath", "snapshotScriptPath",
        "snapshotWrapperPath", "indexLockPath", "timezone", "tableName",
    )
    if any(not isinstance(ownership.get(key), str) or not ownership[key] for key in required_strings):
        raise RuntimeError("Ownership contract fields are incomplete")
    return ownership


def validate_contract(manifest_path: Path, *, create_snapshot_root: bool = False) -> dict[str, Any]:
    ownership = load_ownership(manifest_path, allow_activation_pending=not create_snapshot_root)
    home = Path.home().resolve()
    project = Path(ownership["projectRoot"])
    snapshot_root = Path(ownership["snapshotRoot"])
    receipt = Path(ownership["healthReceiptPath"])
    helper = Path(ownership["snapshotScriptPath"])
    wrapper = Path(ownership["snapshotWrapperPath"])
    lock = Path(ownership["indexLockPath"])
    for path in (project, snapshot_root, receipt, helper, wrapper, lock):
        if not path.is_absolute():
            raise RuntimeError("Ownership contract paths must be absolute")
        _reject_symlink_components(path)
    if project.name != "knowledge-lancedb-qwen-local" or not _inside(project, home):
        raise RuntimeError("Owned Qwen project root is outside the approved boundary")
    expected_script = project / "scripts/snapshot_knowledge_assets.py"
    expected_wrapper = project / "scripts/run_verified_snapshot.py"
    expected_receipt = project / "reports/backup-health-component.qwen-local.json"
    expected_lock = project / "data/index.lock"
    if helper.resolve(strict=False) != expected_script.resolve(strict=False):
        raise RuntimeError("Snapshot helper identity does not match the owned project")
    if wrapper.resolve(strict=False) != expected_wrapper.resolve(strict=False):
        raise RuntimeError("Snapshot wrapper identity does not match the owned project")
    if receipt.resolve(strict=False) != expected_receipt.resolve(strict=False):
        raise RuntimeError("Health receipt identity does not match the owned project")
    if lock.resolve(strict=False) != expected_lock.resolve(strict=False):
        raise RuntimeError("Index lock identity does not match the owned project")
    if helper.is_symlink() or not helper.is_file() or helper.stat().st_uid != os.getuid():
        raise RuntimeError("Snapshot helper is missing or unsafe")
    if not _inside(snapshot_root, home) or snapshot_root == project or _inside(snapshot_root, project):
        raise RuntimeError("Snapshot root is outside the approved private boundary")
    if ownership["tableName"] != TABLE_NAME:
        raise RuntimeError("Snapshot table identity is unsupported")
    if ownership.get("healthReceiptSchema") != HEALTH_RECEIPT_SCHEMA \
            or ownership.get("incrementalDeclarationKey") != INCREMENTAL_DECLARATION_KEY \
            or ownership.get("snapshotDeclarationKey") != SNAPSHOT_DECLARATION_KEY \
            or ownership.get("initialDeclarationKey") != INITIAL_DECLARATION_KEY:
        raise RuntimeError("Snapshot health/declaration contract identity is unsupported")
    try:
        ZoneInfo(ownership["timezone"])
    except Exception as error:
        raise RuntimeError("Snapshot timezone is invalid") from error
    _validate_owned_directory(project)
    _validate_owned_directory(snapshot_root, create=create_snapshot_root, restricted=True)
    source_map = load_json(project / "config/source-map.json")
    embedding = source_map.get("embedding")
    if not isinstance(embedding, dict) or embedding.get("provider") != "qwen-local":
        raise RuntimeError("Snapshot project is not configured for qwen-local")
    endpoint = urlparse(str(embedding.get("endpoint", "")))
    try:
        endpoint_port = endpoint.port
    except ValueError as error:
        raise RuntimeError("Qwen embedding endpoint is malformed") from error
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" \
            or endpoint.username is not None or endpoint.password is not None \
            or endpoint_port is None or endpoint.path not in ("", "/") \
            or endpoint.params or endpoint.query or endpoint.fragment:
        raise RuntimeError("Qwen embedding endpoint must remain loopback-only")
    for key, value in embedding.items():
        if "fallback" in str(key).lower() and value not in (None, False, "", [], {}):
            raise RuntimeError("Cloud or provider fallback is forbidden")
    return ownership


@contextmanager
def snapshot_index_lock(lock: Path, *, wait_seconds: float, poll_seconds: float,
                        sleeper: Callable[[float], None] = time.sleep,
                        clock: Callable[[], float] = time.monotonic):
    """Atomically exclude index writers for the complete snapshot transaction.

    Index wrappers acquire this same directory with ``mkdir``. Merely observing that
    it is absent is not sufficient: an index could start between that observation
    and the first copied asset. The snapshot therefore becomes an equal participant
    in the directory-lock protocol and owns the lock until verification and pruning
    have completed.
    """
    deadline = clock() + max(0.0, wait_seconds)
    identity: str | None = None
    try:
        while identity is None:
            status, acquired_identity = acquire_index_lock(lock)
            if status == "acquired":
                if not acquired_identity:
                    raise RuntimeError("Index lock helper returned no ownership identity")
                identity = acquired_identity
                break
            if status != "busy":
                raise RuntimeError("Index lock helper returned an unsupported state")
            if clock() >= deadline:
                raise TimeoutError("Index lock did not clear before the bounded snapshot deadline")
            sleeper(min(max(0.01, poll_seconds), max(0.01, deadline - clock())))
        yield
    finally:
        if identity is not None:
            release_index_lock(lock, identity)


def trusted_closeout(project: Path, expected_table: str) -> tuple[str, int]:
    state = load_json(project / "data/index-state.json")
    ready = load_json(project / "data/openclaw-ready.json")
    rows = state.get("chunks")
    updated = state.get("updatedAt")
    marked = ready.get("markedAt")
    if type(rows) is not int or rows < 0 or not isinstance(updated, str) or not isinstance(marked, str):
        raise RuntimeError("Qwen closeout state is incomplete")
    if ready.get("ready") is not True or ready.get("provider") != "qwen-local":
        raise RuntimeError("Qwen ready marker is not local-only")
    if state.get("tableName") != expected_table or ready.get("tableName") != expected_table:
        raise RuntimeError("Qwen table identity does not match the snapshot contract")
    if ready.get("chunks") != rows or ready.get("buildFingerprint") != state.get("buildFingerprint"):
        raise RuntimeError("Qwen closeout state and ready marker do not reconcile")
    updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    marked_dt = datetime.fromisoformat(marked.replace("Z", "+00:00"))
    if updated_dt.tzinfo is None or marked_dt.tzinfo is None or marked_dt < updated_dt:
        raise RuntimeError("Qwen closeout timestamps are invalid")
    return marked_dt.astimezone(timezone.utc).isoformat(), rows


def _is_fresh(created_at: str | None, required_after: str) -> bool:
    if not created_at:
        return False
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    required = datetime.fromisoformat(required_after.replace("Z", "+00:00"))
    return created.tzinfo is not None and required.tzinfo is not None and created > required


def verify_complete(project: Path, snapshot: Path, snapshot_root: Path,
                    required_after: str, rows: int, table_name: str) -> dict[str, Any]:
    if snapshot.parent.resolve() != (snapshot_root / "snapshots").resolve():
        raise RuntimeError("Snapshot verification target is outside the owned root")
    result = verify_snapshot(snapshot)
    if not _is_fresh(result.get("createdAt"), required_after):
        raise RuntimeError("Snapshot is older than the latest successful index closeout")
    restore = restore_canary(snapshot)
    database = verify_database(project, snapshot, table_name, rows)
    if restore.get("restoreCanary") is not True or database.get("rowCountPass") is not True:
        raise RuntimeError("Snapshot restore verification did not pass")
    return {"manifest": result, "restore": restore, "database": database}


def _latest_repair(snapshot_root: Path, day_text: str) -> Path | None:
    candidates = sorted((snapshot_root / "snapshots").glob(f"repair-{day_text}-*-post-index"))
    return candidates[-1] if candidates else None


def _new_repair_name(snapshot_root: Path, day_text: str, now: datetime) -> str:
    base = f"repair-{day_text}-{now.strftime('%H%M%S')}-post-index"
    if not (snapshot_root / "snapshots" / base).exists():
        return base
    for suffix in range(1, 10):
        candidate = f"{base}-{suffix}"
        if not (snapshot_root / "snapshots" / candidate).exists():
            return candidate
    raise RuntimeError("Repair snapshot collision limit reached")


def run_snapshot(manifest_path: Path, *, wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
                 poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
                 now: datetime | None = None) -> dict[str, Any]:
    ownership = validate_contract(manifest_path, create_snapshot_root=True)
    project = Path(ownership["projectRoot"])
    snapshot_root = Path(ownership["snapshotRoot"])
    receipt_path = Path(ownership["healthReceiptPath"])
    table_name = ownership["tableName"]
    with snapshot_index_lock(
        Path(ownership["indexLockPath"]),
        wait_seconds=wait_seconds,
        poll_seconds=poll_seconds,
    ):
        with snapshot_run_lock(snapshot_root):
            required_after, rows = trusted_closeout(project, table_name)
            moment = now or datetime.now(ZoneInfo(ownership["timezone"]))
            day_text = moment.astimezone(ZoneInfo(ownership["timezone"])).date().isoformat()
            daily = snapshot_root / "snapshots" / f"daily-{day_text}"
            selected = daily
            created_kind = "daily"
            if daily.exists() or daily.is_symlink():
                if daily.is_symlink() or not daily.is_dir():
                    raise RuntimeError("Existing daily snapshot is unsafe")
                daily_verification = verify_snapshot(daily)
                if _is_fresh(daily_verification.get("createdAt"), required_after):
                    verify_complete(project, daily, snapshot_root, required_after, rows, table_name)
                    created_kind = "reused"
                else:
                    repair = _latest_repair(snapshot_root, day_text)
                    repair_verification = verify_snapshot(repair) if repair is not None else None
                    if repair is not None and _is_fresh(
                        repair_verification.get("createdAt") if repair_verification else None,
                        required_after,
                    ):
                        verify_complete(project, repair, snapshot_root, required_after, rows, table_name)
                        selected = repair
                        created_kind = "repair-reused"
                    else:
                        repair_name = _new_repair_name(snapshot_root, day_text, moment)
                        create_snapshot(project, snapshot_root, repair_name, [required_after])
                        selected = snapshot_root / "snapshots" / repair_name
                        verify_complete(project, selected, snapshot_root, required_after, rows, table_name)
                        created_kind = "repair"
            else:
                create_snapshot(project, snapshot_root, f"daily-{day_text}", [required_after])
                verify_complete(project, daily, snapshot_root, required_after, rows, table_name)
            prune_daily_snapshots(snapshot_root, 30, date.fromisoformat(day_text))
            prune_transient_snapshots(snapshot_root, 7, 10, date.fromisoformat(day_text))
            write_receipt(receipt_path, build_receipt(event="snapshot", status="ok", rows=rows))
            return {"ok": True, "status": "ok", "kind": created_kind, "rows": rows}


def _write_failure_receipt(manifest_path: Path) -> None:
    try:
        ownership = load_ownership(manifest_path)
        write_receipt(
            Path(ownership["healthReceiptPath"]),
            build_receipt(event="snapshot", status="error", anomaly_code="QWEN_SNAPSHOT_FAILED"),
        )
    except Exception:
        pass


def _write_busy_receipt(manifest_path: Path) -> None:
    try:
        ownership = load_ownership(manifest_path)
        write_receipt(
            Path(ownership["healthReceiptPath"]),
            build_receipt(
                event="snapshot", status="warning", anomaly_code="QWEN_SNAPSHOT_RUN_ACTIVE"
            ),
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the verified Qwen local snapshot contract")
    parser.add_argument("--ownership-manifest", required=True)
    parser.add_argument("--check-contract", action="store_true")
    args = parser.parse_args()
    manifest_path = Path(os.path.abspath(Path(args.ownership_manifest).expanduser()))
    try:
        if args.check_contract:
            validate_contract(manifest_path)
            result = {"ok": True, "status": "contract-valid"}
        else:
            result = run_snapshot(manifest_path)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except SnapshotBusy:
        _write_busy_receipt(manifest_path)
        print(json.dumps({"ok": True, "status": "skipped", "reason": "snapshot-run-active"}, sort_keys=True))
        return 0
    except (Exception, SystemExit) as error:
        _write_failure_receipt(manifest_path)
        print(json.dumps({"ok": False, "status": "error", "errorType": type(error).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
