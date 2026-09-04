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


class RecordingCli:
    """Small rollback-only CLI fake that never mutates filesystem targets."""

    def __init__(self) -> None:
        self.executable = str(Path(sys.executable).resolve())
        self.calls: list[list[str]] = []

    def run(
        self, args: list[str], *, timeout: int = 120, check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def json(self, args: list[str], *, timeout: int = 120) -> Any:
        raise AssertionError(f"unexpected JSON CLI call during rollback: {args}")


def _manager(tmp_path: Path) -> core.IntegrationManager:
    home = tmp_path / "home"
    workspace = home / ".openclaw/workspace"
    paths = core.IntegrationPaths(
        home=home,
        workspace=workspace,
        project_root=workspace / "knowledge-lancedb-qwen-local",
        runtime_root=home / "Library/Application Support/OpenClaw/qwen-local",
        state_root=home / "Library/Application Support/OpenClaw/qwen-local-integration",
        launchd_plist=home / "Library/LaunchAgents/ai.openclaw.qwen-local-embedding.plist",
    )
    for directory in (
        paths.workspace,
        paths.project_root,
        paths.runtime_root,
        paths.state_root,
        paths.launchd_plist.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return core.IntegrationManager(
        paths=paths,
        repo_root=Path(__file__).resolve().parents[1],
        cli=RecordingCli(),
        node_path=Path(sys.executable),
        python_path=Path(sys.executable),
        report_channel="discord",
        report_to="channel:1493072746702311474",
        report_account_id="default",
    )


def _write_tree(root: Path, filename: str, content: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(content, encoding="utf-8")
    return path


def _asset_receipt(
    item: core.IntegrationManager,
    *,
    target: Path,
    backup: Path,
    backup_relative_name: str,
) -> dict[str, Any]:
    parent = target.parent.stat()
    target_meta = target.stat()
    backup_meta = backup.stat()
    digest = item._safe_tree_sha256(backup, label="security regression backup")
    return {
        "canonicalParent": str(target.parent),
        "canonicalName": target.name,
        "parentDev": parent.st_dev,
        "parentIno": parent.st_ino,
        "preExisted": True,
        "preKind": "directory",
        "preDev": target_meta.st_dev,
        "preIno": target_meta.st_ino,
        "preMode": stat.S_IMODE(target_meta.st_mode),
        "preSha256": digest,
        "backupRelativeName": backup_relative_name,
        "backupKind": "directory",
        "backupDev": backup_meta.st_dev,
        "backupIno": backup_meta.st_ino,
        "backupMode": stat.S_IMODE(backup_meta.st_mode),
        "backupSha256": digest,
        "mutationStarted": True,
    }


def _record_post_target(
    item: core.IntegrationManager, receipt: dict[str, Any], target: Path,
) -> None:
    metadata = target.stat()
    receipt.update({
        "postKind": "directory",
        "postDev": metadata.st_dev,
        "postIno": metadata.st_ino,
        "postMode": stat.S_IMODE(metadata.st_mode),
        "postSha256": item._safe_tree_sha256(target, label="installed target"),
    })


def _rollback_transaction(
    item: core.IntegrationManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plugin_mutation_started: bool = False,
    skill_mutation_started: bool = False,
) -> tuple[dict[str, Any], Path]:
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    snapshot_dir = (
        item.paths.state_root
        / "snapshots/run-00000000-0000-0000-0000-000000000777"
    )
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    transaction: dict[str, Any] = {
        "schemaVersion": core.SCHEMA_VERSION,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "runId": "asset-rollback-security",
        "phase": "failed",
        "ownedAssets": [],
        "configPath": str(config),
        "configBackupPath": str(snapshot_dir / "openclaw-config.preinstall"),
        "preConfigSha256": "1" * 64,
        "snapshotRunMarkerSha256": "2" * 64,
        "snapshotRunDev": 1,
        "snapshotRunIno": 2,
        "runtimeMutationStarted": True,
        "pluginMutationStarted": plugin_mutation_started,
        "configMutationStarted": False,
        "skillMutationStarted": skill_mutation_started,
        "plistMutationStarted": False,
        "launchdMutationStarted": False,
        "healthReceiptExisted": False,
        "cronMutationStarted": False,
        "cronDefinitionsBefore": [],
        "cronUnknownHashesBefore": {},
        "cronInventoryHashesBefore": {},
        "cronTargetIdsBefore": [],
        "managedCronIdsAfter": [],
        "snapshotRootCreated": False,
        "snapshotLockCreated": False,
        "projectCreated": False,
        "assetRecoverySchema": "qwen-local.rollback-assets.v1",
        "assetReceipts": {},
    }
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _receipt: None)
    return transaction, snapshot_dir


def _attempt_rollback(item: core.IntegrationManager) -> RuntimeError | None:
    try:
        item._rollback_locked(require_exact_post_config=False)
    except RuntimeError as error:
        return error
    return None


def test_skill_backup_tamper_refuses_before_any_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _manager(tmp_path)
    skill_target = item.paths.workspace / "skills" / core.SKILL_ID
    installed = _write_tree(skill_target, "SKILL.md", "installed-current")
    transaction, snapshot_dir = _rollback_transaction(
        item, monkeypatch, skill_mutation_started=True,
    )
    transaction.update(item._snapshot_other_assets(snapshot_dir))
    receipt = _asset_receipt(
        item,
        target=skill_target,
        backup=snapshot_dir / "skill.preinstall",
        backup_relative_name="skill.preinstall",
    )
    _record_post_target(item, receipt, skill_target)
    transaction["assetReceipts"]["skill"] = receipt
    item.store.write(transaction)
    before_identity = (skill_target.stat().st_dev, skill_target.stat().st_ino)
    (snapshot_dir / "skill.preinstall/SKILL.md").write_text(
        "tampered-backup", encoding="utf-8",
    )

    refused = _attempt_rollback(item)

    assert installed.read_text(encoding="utf-8") == "installed-current"
    assert (skill_target.stat().st_dev, skill_target.stat().st_ino) == before_identity
    assert refused is not None, "tampered skill backup must fail closed"


def test_project_backup_tamper_refuses_before_any_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _manager(tmp_path)
    project_src = item.paths.project_root / "src"
    installed = _write_tree(project_src, "module.py", "installed-current")
    transaction, snapshot_dir = _rollback_transaction(item, monkeypatch)
    transaction.update(item._snapshot_other_assets(snapshot_dir))
    receipt = _asset_receipt(
        item,
        target=project_src,
        backup=snapshot_dir / "project-runtime.preinstall/src",
        backup_relative_name="project-runtime.preinstall/src",
    )
    _record_post_target(item, receipt, project_src)
    transaction["assetReceipts"]["project.src"] = receipt
    item.store.write(transaction)
    before_identity = (project_src.stat().st_dev, project_src.stat().st_ino)
    (snapshot_dir / "project-runtime.preinstall/src/module.py").write_text(
        "tampered-backup", encoding="utf-8",
    )

    refused = _attempt_rollback(item)

    assert installed.read_text(encoding="utf-8") == "installed-current"
    assert (project_src.stat().st_dev, project_src.stat().st_ino) == before_identity
    assert refused is not None, "tampered project backup must fail closed"


def _replace_installed_directory(target: Path, marker_name: str) -> Path:
    installed = target.with_name(f".{target.name}.installed-by-integrator")
    target.rename(installed)
    replacement = target.with_name(f".{target.name}.external-replacement")
    marker = _write_tree(replacement, marker_name, "external-replacement")
    replacement.rename(target)
    return marker.with_name(marker.name)


def test_plugin_target_inode_replacement_is_refused_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _manager(tmp_path)
    plugin_target = item.plugin_target
    _write_tree(plugin_target, "index.js", "preinstall-plugin")
    transaction, snapshot_dir = _rollback_transaction(
        item, monkeypatch, plugin_mutation_started=True,
    )
    transaction.update(item._snapshot_other_assets(snapshot_dir))
    receipt = _asset_receipt(
        item,
        target=plugin_target,
        backup=snapshot_dir / "plugin.preinstall",
        backup_relative_name="plugin.preinstall",
    )
    shutil.rmtree(plugin_target)
    _write_tree(plugin_target, "index.js", "installed-plugin")
    _record_post_target(item, receipt, plugin_target)
    transaction["assetReceipts"]["plugin"] = receipt
    item.store.write(transaction)
    marker = _replace_installed_directory(plugin_target, "external.txt")

    refused = _attempt_rollback(item)

    canonical_marker = plugin_target / marker.name
    assert canonical_marker.is_file(), "replacement plugin target was deleted"
    assert canonical_marker.read_text(encoding="utf-8") == "external-replacement"
    assert refused is not None, "replaced plugin target must be refused"


def test_skill_target_inode_replacement_is_refused_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _manager(tmp_path)
    skill_target = item.paths.workspace / "skills" / core.SKILL_ID
    _write_tree(skill_target, "SKILL.md", "preinstall-skill")
    transaction, snapshot_dir = _rollback_transaction(
        item, monkeypatch, skill_mutation_started=True,
    )
    transaction.update(item._snapshot_other_assets(snapshot_dir))
    receipt = _asset_receipt(
        item,
        target=skill_target,
        backup=snapshot_dir / "skill.preinstall",
        backup_relative_name="skill.preinstall",
    )
    shutil.rmtree(skill_target)
    _write_tree(skill_target, "SKILL.md", "installed-skill")
    _record_post_target(item, receipt, skill_target)
    transaction["assetReceipts"]["skill"] = receipt
    item.store.write(transaction)
    marker = _replace_installed_directory(skill_target, "external.txt")

    refused = _attempt_rollback(item)

    canonical_marker = skill_target / marker.name
    assert canonical_marker.is_file(), "replacement skill target was deleted"
    assert canonical_marker.read_text(encoding="utf-8") == "external-replacement"
    assert refused is not None, "replaced skill target must be refused"


def test_project_target_inode_replacement_is_refused_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _manager(tmp_path)
    project_src = item.paths.project_root / "src"
    _write_tree(project_src, "module.py", "preinstall-project")
    transaction, snapshot_dir = _rollback_transaction(item, monkeypatch)
    transaction.update(item._snapshot_other_assets(snapshot_dir))
    receipt = _asset_receipt(
        item,
        target=project_src,
        backup=snapshot_dir / "project-runtime.preinstall/src",
        backup_relative_name="project-runtime.preinstall/src",
    )
    shutil.rmtree(project_src)
    _write_tree(project_src, "module.py", "installed-project")
    _record_post_target(item, receipt, project_src)
    transaction["assetReceipts"]["project.src"] = receipt
    item.store.write(transaction)
    marker = _replace_installed_directory(project_src, "external.txt")

    refused = _attempt_rollback(item)

    canonical_marker = project_src / marker.name
    assert canonical_marker.is_file(), "replacement project target was deleted"
    assert canonical_marker.read_text(encoding="utf-8") == "external-replacement"
    assert refused is not None, "replaced project target must be refused"
