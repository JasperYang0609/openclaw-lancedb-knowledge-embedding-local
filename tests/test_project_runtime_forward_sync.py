from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import src.openclaw_integration.core as core
from test_openclaw_reconciliation_v2 import manager


class SimulatedCrash(RuntimeError):
    pass


PROJECT_ASSETS = {
    "project.src": Path("src"),
    "project.scripts": Path("scripts"),
    "project.package_json": Path("package.json"),
    "project.package_lock": Path("package-lock.json"),
}


def _write_asset(root: Path, relative: Path, value: str) -> None:
    path = root / relative
    if relative.suffix == ".json":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return
    path.mkdir(parents=True, exist_ok=True)
    (path / "marker.txt").write_text(value, encoding="utf-8")


def _asset_value(root: Path, relative: Path) -> str:
    path = root / relative
    return (
        path.read_text(encoding="utf-8")
        if relative.suffix == ".json"
        else (path / "marker.txt").read_text(encoding="utf-8")
    )


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    absent_asset: str | None = None,
) -> tuple[core.IntegrationManager, dict[str, Any], Path]:
    item = manager(tmp_path)
    skill = item.paths.home / "bundle-skill"
    item.skill_source = skill
    template = skill / "assets/knowledge-lancedb-template"
    for asset_id, relative in PROJECT_ASSETS.items():
        _write_asset(template, relative, f"new:{asset_id}")
        if asset_id != absent_asset:
            _write_asset(item.paths.project_root, relative, f"old:{asset_id}")

    snapshot_dir = item.paths.state_root / "snapshots/run-forward-sync"
    snapshot_dir.mkdir(parents=True, mode=0o700)
    snapshot = item._snapshot_other_assets(snapshot_dir)
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    transaction: dict[str, Any] = {
        "schemaVersion": core.SCHEMA_VERSION,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "forward-sync-test",
        "phase": "staging",
        "ownership": item._ownership_payload(),
        "ownedAssets": [],
        "configPath": str(config),
        "configBackupPath": str(snapshot_dir / "openclaw-config.preinstall"),
        "preConfigSha256": "1" * 64,
        "snapshotRunMarkerSha256": "2" * 64,
        "snapshotRunDev": 1,
        "snapshotRunIno": 2,
        "runtimeMutationStarted": True,
        "pluginMutationStarted": False,
        "configMutationStarted": False,
        "skillMutationStarted": False,
        "projectRuntimeMutationStarted": False,
        "plistMutationStarted": False,
        "launchdMutationStarted": False,
        "healthReceiptExisted": False,
        "cronMutationStarted": False,
        "cronDefinitionsBefore": [],
        "cronUnknownHashesBefore": {},
        "cronInventoryHashesBefore": {},
        "cronTargetIdsBefore": [],
        "managedCronIdsAfter": [],
        "snapshotRootCreatePlanned": False,
        "snapshotRootCreated": False,
        "snapshotLockCreated": False,
        "projectCreatePlanned": False,
        "projectCreated": False,
        **snapshot,
    }
    item.store.write(transaction)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/true")
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    return item, transaction, snapshot_dir


def _private_artifacts(project_root: Path) -> list[str]:
    return sorted(
        child.name
        for child in project_root.iterdir()
        if child.name.startswith(".qwen-asset-")
        or child.name.startswith(".qwen-recovery-quarantine-")
    )


def _crash_after_checkpoint(
    item: core.IntegrationManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str,
    asset_id: str = "project.src",
) -> None:
    original = item._checkpoint_asset_receipt
    crashed = False

    def checkpoint(
        transaction: dict[str, Any], current_asset_id: str, updates: dict[str, Any],
    ) -> None:
        nonlocal crashed
        original(transaction, current_asset_id, updates)
        if not crashed and current_asset_id == asset_id and key in updates:
            crashed = True
            raise SimulatedCrash(key)

    monkeypatch.setattr(item, "_checkpoint_asset_receipt", checkpoint)


def _rollback_supported_path(
    item: core.IntegrationManager,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _receipt: None)
    return item._rollback_locked(require_exact_post_config=False)


def test_forward_sync_atomically_replaces_directory_and_file_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, transaction, _snapshot_dir = _prepare(tmp_path, monkeypatch)

    item.synchronize_project_runtime(transaction)

    for asset_id, relative in PROJECT_ASSETS.items():
        assert _asset_value(item.paths.project_root, relative) == f"new:{asset_id}"
        receipt = transaction["assetReceipts"][asset_id]
        assert receipt["installPublished"] is True
        assert receipt["installComplete"] is True
        assert receipt["postSha256"] == receipt["installStageSha256"]
    assert _private_artifacts(item.paths.project_root) == []


@pytest.mark.parametrize(
    "checkpoint",
    [
        "installStageName",
        "installQuarantined",
        "installPublished",
        "installQuarantinePurged",
    ],
)
def test_forward_sync_retries_each_durable_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    item, transaction, _snapshot_dir = _prepare(tmp_path, monkeypatch)
    original = item._checkpoint_asset_receipt
    _crash_after_checkpoint(item, monkeypatch, key=checkpoint)

    with pytest.raises(SimulatedCrash, match=checkpoint):
        item.synchronize_project_runtime(transaction)

    monkeypatch.setattr(item, "_checkpoint_asset_receipt", original)
    recovered = item.store.read()
    item.synchronize_project_runtime(recovered)

    assert _asset_value(item.paths.project_root, Path("src")) == "new:project.src"
    assert recovered["assetReceipts"]["project.src"]["installComplete"] is True
    assert _private_artifacts(item.paths.project_root) == []


@pytest.mark.parametrize(
    "checkpoint",
    ["installStageName", "installQuarantined", "installPublished"],
)
def test_supported_rollback_recovers_each_forward_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    item, transaction, _snapshot_dir = _prepare(tmp_path, monkeypatch)
    original = item._checkpoint_asset_receipt
    _crash_after_checkpoint(item, monkeypatch, key=checkpoint)
    with pytest.raises(SimulatedCrash, match=checkpoint):
        item.synchronize_project_runtime(transaction)
    monkeypatch.setattr(item, "_checkpoint_asset_receipt", original)

    result = _rollback_supported_path(item, monkeypatch)

    assert result["status"] == "ROLLED_BACK"
    for asset_id, relative in PROJECT_ASSETS.items():
        assert _asset_value(item.paths.project_root, relative) == f"old:{asset_id}"
    assert _private_artifacts(item.paths.project_root) == []


@pytest.mark.parametrize(
    ("asset_id", "relative"),
    [("project.src", Path("src")), ("project.package_json", Path("package.json"))],
)
def test_forward_sync_preserves_replacement_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asset_id: str,
    relative: Path,
) -> None:
    item, transaction, _snapshot_dir = _prepare(tmp_path, monkeypatch)
    original = item._checkpoint_asset_receipt
    replaced = False

    def replace_after_stage(
        current_transaction: dict[str, Any], current_asset_id: str, updates: dict[str, Any],
    ) -> None:
        nonlocal replaced
        original(current_transaction, current_asset_id, updates)
        if not replaced and current_asset_id == asset_id and "installStageName" in updates:
            replaced = True
            target = item.paths.project_root / relative
            held = item.paths.project_root / f"held-{relative.name}"
            os.rename(target, held)
            _write_asset(item.paths.project_root, relative, "replacement")

    monkeypatch.setattr(item, "_checkpoint_asset_receipt", replace_after_stage)

    with pytest.raises(RuntimeError, match="target changed"):
        item.synchronize_project_runtime(transaction)

    assert _asset_value(item.paths.project_root, relative) == "replacement"
    assert _asset_value(item.paths.project_root, Path(f"held-{relative.name}")) \
        == f"old:{asset_id}"


def test_forward_sync_preserves_absent_target_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = "project.package_lock"
    relative = PROJECT_ASSETS[asset_id]
    item, transaction, _snapshot_dir = _prepare(
        tmp_path, monkeypatch, absent_asset=asset_id,
    )
    original = item._checkpoint_asset_receipt

    def collide_after_stage(
        current_transaction: dict[str, Any], current_asset_id: str, updates: dict[str, Any],
    ) -> None:
        original(current_transaction, current_asset_id, updates)
        if current_asset_id == asset_id and "installStageName" in updates:
            _write_asset(item.paths.project_root, relative, "collision")

    monkeypatch.setattr(item, "_checkpoint_asset_receipt", collide_after_stage)

    with pytest.raises(RuntimeError, match="collision was preserved"):
        item.synchronize_project_runtime(transaction)

    assert _asset_value(item.paths.project_root, relative) == "collision"


def test_forward_sync_detects_parent_replacement_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, transaction, _snapshot_dir = _prepare(tmp_path, monkeypatch)
    original = item._checkpoint_asset_receipt
    replaced = False
    original_root = item.paths.project_root.with_name("held-project-root")

    def replace_parent_after_stage(
        current_transaction: dict[str, Any], current_asset_id: str, updates: dict[str, Any],
    ) -> None:
        nonlocal replaced
        original(current_transaction, current_asset_id, updates)
        if not replaced and current_asset_id == "project.src" \
                and "installStageName" in updates:
            replaced = True
            os.rename(item.paths.project_root, original_root)
            item.paths.project_root.mkdir()
            _write_asset(item.paths.project_root, Path("src"), "replacement-parent")

    monkeypatch.setattr(item, "_checkpoint_asset_receipt", replace_parent_after_stage)

    with pytest.raises(RuntimeError, match="parent for project.src changed"):
        item.synchronize_project_runtime(transaction)

    assert _asset_value(item.paths.project_root, Path("src")) == "replacement-parent"
    assert _asset_value(original_root, Path("src")) == "old:project.src"


def test_forward_sync_detects_source_drift_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, transaction, _snapshot_dir = _prepare(tmp_path, monkeypatch)
    original = item._checkpoint_asset_receipt
    drifted = False
    source_marker = (
        item.skill_source / "assets/knowledge-lancedb-template/src/marker.txt"
    )

    def drift_after_stage(
        current_transaction: dict[str, Any], current_asset_id: str, updates: dict[str, Any],
    ) -> None:
        nonlocal drifted
        original(current_transaction, current_asset_id, updates)
        if not drifted and current_asset_id == "project.src" \
                and "installStageName" in updates:
            drifted = True
            source_marker.write_text("drifted", encoding="utf-8")

    monkeypatch.setattr(item, "_checkpoint_asset_receipt", drift_after_stage)

    with pytest.raises(RuntimeError, match="source changed before publication"):
        item.synchronize_project_runtime(transaction)

    assert _asset_value(item.paths.project_root, Path("src")) == "old:project.src"


def test_forward_sync_requires_durable_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, _transaction, _snapshot_dir = _prepare(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="durable transaction"):
        item.synchronize_project_runtime(None)


def test_install_stage_collision_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, transaction, _snapshot_dir = _prepare(tmp_path, monkeypatch)
    collision_name = ".qwen-asset-install-" + "a" * 32
    collision = item.paths.project_root / collision_name
    collision.mkdir()
    (collision / "marker.txt").write_text("collision", encoding="utf-8")
    monkeypatch.setattr(core.uuid, "uuid4", lambda: type("Fixed", (), {"hex": "a" * 32})())

    with pytest.raises(RuntimeError, match="install stage collided"):
        item.synchronize_project_runtime(transaction)

    assert (collision / "marker.txt").read_text(encoding="utf-8") == "collision"
