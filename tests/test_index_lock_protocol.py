from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "openclaw-lancedb-knowledge-local/assets/knowledge-lancedb-template/scripts"


def load_helper():
    spec = importlib.util.spec_from_file_location("qwen_index_lock_helper", SCRIPTS / "index_lock.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


lock_helper = load_helper()


def runtime_fixture(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "knowledge-lancedb-qwen-local"
    scripts = project / "scripts"
    data = project / "data"
    state = tmp_path / "integration-state"
    scripts.mkdir(parents=True)
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    state.mkdir(mode=0o700)
    for name in (
        "backup_health_component.py",
        "index_lock.py",
        "knowledge_index_full.sh",
        "knowledge_index_incremental.sh",
    ):
        shutil.copy2(SCRIPTS / name, scripts / name)
    ownership = {
        "schema": "qwen-local-openclaw.v2",
        "snapshotContract": "qwen-local-verified-snapshot.v1",
        "provider": "qwen-local",
        "localOnly": True,
        "projectRoot": str(project),
        "snapshotRoot": str(tmp_path / "snapshots"),
        "healthReceiptPath": str(project / "reports/backup-health-component.qwen-local.json"),
        "snapshotScriptPath": str(project / "scripts/snapshot_knowledge_assets.py"),
        "snapshotWrapperPath": str(project / "scripts/run_verified_snapshot.py"),
        "indexLockPath": str(project / "data/index.lock"),
        "timezone": "Asia/Taipei",
        "tableName": "knowledge_chunks_qwen_local_768",
        "healthReceiptSchema": "backup-health-component.v1",
        "incrementalDeclarationKey": "openclaw-lancedb-knowledge-local-incremental-v1",
        "snapshotDeclarationKey": "openclaw-lancedb-knowledge-local-snapshot-v1",
        "initialDeclarationKey": "openclaw-lancedb-knowledge-local-initial-v1",
    }
    manifest = state / "transaction.json"
    manifest.write_text(
        json.dumps({"schemaVersion": 1, "phase": "committed", "ownership": ownership}),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    return project, manifest


def run_wrapper(project: Path, manifest: Path, name: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({
        "OPENCLAW_LANCEDB_ROOT": str(project),
        "QWEN_OWNERSHIP_MANIFEST": str(manifest),
        "QWEN_PYTHON": sys.executable,
    })
    return subprocess.run(
        ["bash", str(project / "scripts" / name), str(manifest)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=15,
    )


def test_lock_helper_acquires_releases_and_accepts_owner_safe_0755_parent(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    lock = data / "index.lock"

    status, identity = lock_helper.acquire(lock)
    assert status == "acquired" and identity
    assert lock.is_dir()
    assert lock_helper.acquire(lock) == ("busy", None)
    lock_helper.release(lock, identity)
    assert not lock.exists()


def test_lock_helper_release_refuses_wrong_identity_without_removing_lock(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    lock = data / "index.lock"
    status, identity = lock_helper.acquire(lock)
    assert status == "acquired" and identity

    with pytest.raises(lock_helper.UnsafeLock, match="identity changed"):
        lock_helper.release(lock, "1:1")

    assert lock.is_dir()
    lock_helper.release(lock, identity)
    assert not lock.exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "world-writable-directory"])
def test_lock_helper_rejects_unsafe_existing_nodes(tmp_path: Path, kind: str) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    lock = data / "index.lock"
    if kind == "file":
        lock.write_text("not a lock", encoding="utf-8")
    elif kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        lock.symlink_to(outside, target_is_directory=True)
    else:
        lock.mkdir()
        lock.chmod(0o777)

    with pytest.raises(lock_helper.UnsafeLock):
        lock_helper.acquire(lock)


def test_lock_helper_non_eexist_creation_failure_is_not_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)

    def denied(*_args, **_kwargs):
        raise PermissionError("fixture denied")

    monkeypatch.setattr(lock_helper.os, "mkdir", denied)
    with pytest.raises(lock_helper.UnsafeLock, match="could not be created"):
        lock_helper.acquire(data / "index.lock")


@pytest.mark.parametrize(
    ("wrapper", "expected_rc"),
    [("knowledge_index_incremental.sh", 0), ("knowledge_index_full.sh", 75)],
)
def test_wrappers_treat_only_real_safe_directory_as_contention(
    tmp_path: Path, wrapper: str, expected_rc: int,
) -> None:
    project, manifest = runtime_fixture(tmp_path)
    lock = project / "data/index.lock"
    lock.mkdir(mode=0o700)

    result = run_wrapper(project, manifest, wrapper)

    assert result.returncode == expected_rc
    assert "another indexing run is active" in result.stdout
    assert lock.is_dir()
    assert not (project / "reports/backup-health-component.qwen-local.json").exists()


@pytest.mark.parametrize("wrapper", ["knowledge_index_incremental.sh", "knowledge_index_full.sh"])
@pytest.mark.parametrize("failure", ["file", "symlink", "permission"])
def test_wrappers_fail_and_write_error_receipt_for_unsafe_or_unavailable_lock(
    tmp_path: Path, wrapper: str, failure: str,
) -> None:
    project, manifest = runtime_fixture(tmp_path)
    data = project / "data"
    lock = data / "index.lock"
    if failure == "file":
        lock.write_text("unsafe", encoding="utf-8")
    elif failure == "symlink":
        outside = tmp_path / "outside-lock"
        outside.mkdir()
        lock.symlink_to(outside, target_is_directory=True)
    else:
        data.chmod(0o555)

    try:
        result = run_wrapper(project, manifest, wrapper)
    finally:
        data.chmod(0o755)

    assert result.returncode != 0
    assert "unsafe or unavailable" in result.stderr
    receipt = json.loads(
        (project / "reports/backup-health-component.qwen-local.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "error"
    expected_key = (
        "openclaw-lancedb-knowledge-local-incremental-v1"
        if wrapper == "knowledge_index_incremental.sh"
        else "openclaw-lancedb-knowledge-local-initial-v1"
    )
    assert receipt["declarationKey"] == expected_key
