from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import src.openclaw_integration.core as core
from test_openclaw_reconciliation_v2 import (
    StatefulCronCli,
    _write_precise_runtime_transaction,
    job_for_spec,
    manager,
)


class LegacyVerificationCli(StatefulCronCli):
    """Expose only the read-only surfaces used by legacy verification."""

    def json(self, args: list[str], *, timeout: int = 120) -> Any:
        if args[:2] == ["plugins", "inspect"]:
            self.calls.append(list(args))
            return {"id": core.PLUGIN_ID, "tools": [core.TOOL_NAME]}
        if args[:2] == ["skills", "info"]:
            self.calls.append(list(args))
            return {"id": core.SKILL_ID, "eligible": True}
        if args[:2] == ["gateway", "status"]:
            self.calls.append(list(args))
            return {"ok": True}
        return super().json(args, timeout=timeout)


class RollbackRecordingCli:
    """Record any control-plane mutation attempted by rollback."""

    def __init__(self) -> None:
        self.executable = str(Path(sys.executable).resolve())
        self.calls: list[list[str]] = []

    def run(
        self, args: list[str], *, timeout: int = 120, check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def json(self, args: list[str], *, timeout: int = 120) -> Any:
        self.calls.append(list(args))
        if args[:4] == ["cron", "list", "--all", "--json"]:
            return {"jobs": [], "total": 0, "hasMore": False, "nextCursor": None}
        raise AssertionError(f"unexpected JSON call during fail-closed rollback: {args}")


def _write_directory(root: Path, filename: str, value: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    marker = root / filename
    marker.write_text(value, encoding="utf-8")
    return marker


def _directory_state(root: Path, marker: Path) -> tuple[int, int, int, bytes]:
    metadata = root.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        marker.read_bytes(),
    )


def _rollback_without_raising_test_process(
    item: core.IntegrationManager,
) -> BaseException | None:
    try:
        item._rollback_locked(require_exact_post_config=False)
    except BaseException as error:  # The assertions below distinguish RuntimeError.
        return error
    return None


def test_contract_and_ownership_schema_are_v3(tmp_path: Path) -> None:
    item = manager(tmp_path)

    assert core.INTEGRATION_CONTRACT_VERSION == 3
    assert core.OWNERSHIP_SCHEMA == "qwen-local-openclaw.v3"
    assert core.ASSET_ROLLBACK_RECEIPT_SCHEMA == "qwen-local.rollback-assets.v1"
    assert item._ownership_payload()["contractVersion"] == 3
    assert item._ownership_payload()["schema"] == "qwen-local-openclaw.v3"


def test_committed_v2_reports_upgrade_required_not_already_current(
    tmp_path: Path,
) -> None:
    cli = LegacyVerificationCli()
    item = manager(tmp_path, cli)
    cli.jobs = [
        job_for_spec(item._incremental_spec(), job_id="legacy-v2-incremental", enabled=True)
    ]
    item.store.write({
        "schemaVersion": core.SCHEMA_VERSION,
        "contractVersion": 2,
        "runId": "committed-v2",
        "phase": "committed",
        "ownership": {
            **item._ownership_payload(),
            "schema": "qwen-local-openclaw.v2",
            "contractVersion": 2,
        },
        "indexState": "READY",
    })

    result = item.verify()

    assert result["ok"] is True
    assert result["contractVersion"] == 2
    assert result["upgradeRequired"] is True
    assert result.get("transaction") != "already_current"


def test_unfinished_v2_runtime_mutation_fails_before_any_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = RollbackRecordingCli()
    item = manager(tmp_path, cli)
    transaction = _write_precise_runtime_transaction(
        item,
        cron_mutation_started=True,
        pluginMutationStarted=True,
        configMutationStarted=True,
        skillMutationStarted=True,
        plistMutationStarted=True,
        launchdMutationStarted=True,
        projectExisted=True,
    )
    transaction["contractVersion"] = 2
    transaction["ownership"] = {
        **item._ownership_payload(),
        "schema": "qwen-local-openclaw.v2",
        "contractVersion": 2,
    }
    project_src = item.paths.project_root / "src"
    project_marker = _write_directory(project_src, "current.txt", "do-not-touch")
    item.store.write(transaction)
    transaction_before = item.store.manifest_path.read_bytes()
    project_before = _directory_state(project_src, project_marker)
    external_events: list[str] = []
    monkeypatch.setattr(
        item, "deactivate_launchd", lambda: external_events.append("deactivate-launchd"),
    )
    monkeypatch.setattr(
        item,
        "_verify_config_snapshot",
        lambda *_args, **_kwargs: external_events.append("verify-config-snapshot"),
    )
    monkeypatch.setattr(
        item,
        "_restore_config_file",
        lambda *_args, **_kwargs: external_events.append("restore-config"),
    )
    monkeypatch.setattr(
        item,
        "_remove_created_snapshot_artifacts",
        lambda *_args, **_kwargs: external_events.append("filesystem-cleanup"),
    )

    error = _rollback_without_raising_test_process(item)

    assert _directory_state(project_src, project_marker) == project_before
    assert item.store.manifest_path.read_bytes() == transaction_before
    assert cli.calls == []
    assert external_events == []
    assert isinstance(error, RuntimeError)


@pytest.mark.parametrize("asset", ["plugin", "skill", "project"])
def test_v3_missing_asset_receipt_fails_before_any_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asset: str,
) -> None:
    cli = RollbackRecordingCli()
    item = manager(tmp_path, cli)
    transaction = _write_precise_runtime_transaction(item)
    snapshot_dir = Path(transaction["configBackupPath"]).parent

    if asset == "plugin":
        target = item.plugin_target
        marker = _write_directory(target, "index.js", "installed-current")
        backup = snapshot_dir / "plugin.preinstall"
        _write_directory(backup, "index.js", "preinstall-backup")
        transaction.update({
            "pluginMutationStarted": True,
            "pluginTargetPath": str(target),
            "pluginBackupPath": str(backup),
            "pluginExisted": True,
            "pluginBackupSha256": item._safe_tree_sha256(
                backup, label="legacy flat plugin backup",
            ),
        })
    elif asset == "skill":
        target = item.paths.workspace / "skills" / core.SKILL_ID
        marker = _write_directory(target, "SKILL.md", "installed-current")
        backup = snapshot_dir / "skill.preinstall"
        _write_directory(backup, "SKILL.md", "preinstall-backup")
        transaction.update({
            "skillMutationStarted": True,
            "skillTargetPath": str(target),
            "skillBackupPath": str(backup),
            "skillExisted": True,
        })
    else:
        target = item.paths.project_root / "src"
        marker = _write_directory(target, "current.txt", "installed-current")
        backup = snapshot_dir / "project-runtime.preinstall" / "src"
        _write_directory(backup, "current.txt", "preinstall-backup")
        transaction.update({
            "projectRuntimeMutationStarted": True,
            "projectExisted": True,
            "projectBackupPath": str(snapshot_dir / "project-runtime.preinstall"),
        })

    item.store.write(transaction)
    transaction_before = item.store.manifest_path.read_bytes()
    target_before = _directory_state(target, marker)
    config_path = Path(transaction["configPath"])
    config_before = config_path.read_bytes()
    external_events: list[str] = []
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        item, "deactivate_launchd", lambda: external_events.append("deactivate-launchd"),
    )
    monkeypatch.setattr(
        item,
        "_restore_regular_file",
        lambda *_args, **_kwargs: external_events.append("restore-regular-file"),
    )
    monkeypatch.setattr(
        item,
        "_restore_config_file",
        lambda *_args, **_kwargs: external_events.append("restore-config"),
    )
    monkeypatch.setattr(
        item,
        "_remove_created_snapshot_artifacts",
        lambda *_args, **_kwargs: external_events.append("filesystem-cleanup"),
    )

    error = _rollback_without_raising_test_process(item)

    assert _directory_state(target, marker) == target_before
    assert config_path.read_bytes() == config_before
    assert item.store.manifest_path.read_bytes() == transaction_before
    assert cli.calls == []
    assert external_events == []
    assert isinstance(error, RuntimeError)
