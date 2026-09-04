from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

import src.openclaw_integration.core as core
from test_openclaw_reconciliation_v2 import (
    _write_precise_runtime_transaction,
    manager,
)


class SimulatedCrash(BaseException):
    """Model an uncatchable process stop between a filesystem step and its receipt."""


def _write_directory(
    root: Path,
    files: dict[str, bytes],
    *,
    root_mode: int = 0o750,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(root_mode)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o640)


def _write_file(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)


def _replace_directory(root: Path, files: dict[str, bytes]) -> None:
    shutil.rmtree(root)
    _write_directory(root, files, root_mode=0o700)


def _asset_state(path: Path) -> tuple[Any, ...]:
    if path.is_file():
        metadata = path.stat()
        return ("file", stat.S_IMODE(metadata.st_mode), path.read_bytes())
    entries: list[tuple[Any, ...]] = [
        (".", "directory", stat.S_IMODE(path.stat().st_mode)),
    ]
    for candidate in sorted(path.rglob("*")):
        relative = candidate.relative_to(path).as_posix()
        metadata = candidate.stat()
        if candidate.is_dir():
            entries.append((relative, "directory", stat.S_IMODE(metadata.st_mode)))
        else:
            entries.append(
                (
                    relative,
                    "file",
                    stat.S_IMODE(metadata.st_mode),
                    candidate.read_bytes(),
                )
            )
    return tuple(entries)


def _prepare_transaction(
    item: core.IntegrationManager,
) -> tuple[dict[str, Any], Path]:
    transaction = _write_precise_runtime_transaction(item)
    snapshot_dir = Path(transaction["configBackupPath"]).parent
    transaction.update(item._snapshot_other_assets(snapshot_dir))
    item.store.write(transaction)
    return transaction, snapshot_dir


def _stub_non_asset_rollback(
    item: core.IntegrationManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        item,
        "_remove_created_snapshot_artifacts",
        lambda _transaction: None,
    )


def _run_rollback(
    item: core.IntegrationManager,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    _stub_non_asset_rollback(item, monkeypatch)
    return item._rollback_locked(require_exact_post_config=False)


def _mutate_and_record(
    item: core.IntegrationManager,
    transaction: dict[str, Any],
    asset_ids: list[str],
) -> None:
    item._checkpoint_asset_mutation(transaction, asset_ids)
    for asset_id in asset_ids:
        item._capture_asset_post_identity(transaction, asset_id)


def _assert_no_recovery_residue(parent: Path) -> None:
    assert not [
        candidate.name
        for candidate in parent.iterdir()
        if candidate.name.startswith(".qwen-asset-restore-")
        or candidate.name.startswith(".qwen-recovery-quarantine-")
    ]


def test_v3_rollback_restores_preexisting_skill_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    skill = item.paths.workspace / "skills" / core.SKILL_ID
    _write_directory(
        skill,
        {
            "SKILL.md": b"preinstall skill\n",
            "references/policy.md": b"preinstall policy\n",
        },
    )
    expected = _asset_state(skill)
    transaction, _snapshot_dir = _prepare_transaction(item)
    item._checkpoint_asset_mutation(transaction, ["skill"])
    _replace_directory(skill, {"SKILL.md": b"installed replacement\n"})
    item._capture_asset_post_identity(transaction, "skill")

    result = _run_rollback(item, monkeypatch)

    assert result == {
        "ok": True,
        "status": "ROLLED_BACK",
        "outcome": "restored_exactly",
    }
    assert _asset_state(skill) == expected
    receipt = item.store.read()["assetReceipts"]["skill"]
    assert receipt["rollbackComplete"] is True
    _assert_no_recovery_residue(skill.parent)


def test_v3_rollback_restores_all_four_preexisting_project_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    project = item.paths.project_root
    src = project / "src"
    scripts = project / "scripts"
    package_json = project / "package.json"
    package_lock = project / "package-lock.json"
    _write_directory(src, {"app.py": b"print('preinstall')\n"})
    _write_directory(scripts, {"run.sh": b"#!/bin/sh\necho preinstall\n"})
    (scripts / "run.sh").chmod(0o750)
    _write_file(package_json, b'{"version":"1.0.0"}\n', mode=0o640)
    _write_file(package_lock, b'{"lockfileVersion":3}\n', mode=0o600)
    targets = {
        "project.src": src,
        "project.scripts": scripts,
        "project.package_json": package_json,
        "project.package_lock": package_lock,
    }
    expected = {asset_id: _asset_state(path) for asset_id, path in targets.items()}
    previous_umask = os.umask(0o077)
    try:
        transaction, _snapshot_dir = _prepare_transaction(item)
    finally:
        os.umask(previous_umask)
    asset_ids = list(targets)
    item._checkpoint_asset_mutation(transaction, asset_ids)
    _replace_directory(src, {"app.py": b"print('installed')\n"})
    _replace_directory(scripts, {"run.sh": b"#!/bin/sh\necho installed\n"})
    _write_file(package_json, b'{"version":"2.0.0"}\n', mode=0o600)
    _write_file(package_lock, b'{"lockfileVersion":4}\n', mode=0o640)
    for asset_id in asset_ids:
        item._capture_asset_post_identity(transaction, asset_id)

    result = _run_rollback(item, monkeypatch)

    assert result["status"] == "ROLLED_BACK"
    assert result["outcome"] == "restored_exactly"
    assert {
        asset_id: _asset_state(path) for asset_id, path in targets.items()
    } == expected
    receipts = item.store.read()["assetReceipts"]
    assert all(receipts[asset_id]["rollbackComplete"] is True for asset_id in asset_ids)
    _assert_no_recovery_residue(project)


def test_v3_rollback_removes_only_the_receipted_new_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    skill = item.paths.workspace / "skills" / core.SKILL_ID
    skill.parent.mkdir(parents=True, exist_ok=True)
    neighbour = skill.parent / "keep-me"
    _write_directory(neighbour, {"marker.txt": b"preserve\n"})
    neighbour_before = _asset_state(neighbour)
    transaction, _snapshot_dir = _prepare_transaction(item)
    assert transaction["assetReceipts"]["skill"]["preExisted"] is False
    item._checkpoint_asset_mutation(transaction, ["skill"])
    _write_directory(skill, {"SKILL.md": b"new install\n"})
    item._capture_asset_post_identity(transaction, "skill")

    result = _run_rollback(item, monkeypatch)

    assert result["status"] == "ROLLED_BACK"
    assert not os.path.lexists(skill)
    assert _asset_state(neighbour) == neighbour_before
    receipt = item.store.read()["assetReceipts"]["skill"]
    assert receipt["rollbackComplete"] is True
    assert receipt["quarantinePurged"] is True
    _assert_no_recovery_residue(skill.parent)


@pytest.mark.parametrize(
    ("crash_receipt_key", "expected_state"),
    [
        ("restoreStageKind", "stage-created"),
        ("quarantined", "target-quarantined"),
        ("rollbackPublished", "restore-published"),
        ("quarantinePurged", "quarantine-purged"),
    ],
)
def test_v3_rollback_resumes_after_filesystem_step_precedes_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_receipt_key: str,
    expected_state: str,
) -> None:
    item = manager(tmp_path)
    skill = item.paths.workspace / "skills" / core.SKILL_ID
    _write_directory(skill, {"SKILL.md": b"preinstall skill\n"})
    expected = _asset_state(skill)
    transaction, snapshot_dir = _prepare_transaction(item)
    item._checkpoint_asset_mutation(transaction, ["skill"])
    _replace_directory(skill, {"SKILL.md": b"installed replacement\n"})
    item._capture_asset_post_identity(transaction, "skill")
    _stub_non_asset_rollback(item, monkeypatch)
    original_checkpoint = item._checkpoint_asset_receipt
    crashed = False

    def crash_before_receipt(
        pending: dict[str, Any], asset_id: str, updates: dict[str, Any],
    ) -> None:
        nonlocal crashed
        if not crashed and asset_id == "skill" and crash_receipt_key in updates:
            crashed = True
            raise SimulatedCrash(expected_state)
        original_checkpoint(pending, asset_id, updates)

    monkeypatch.setattr(item, "_checkpoint_asset_receipt", crash_before_receipt)

    with pytest.raises(SimulatedCrash, match=expected_state):
        item._rollback_locked(require_exact_post_config=False)

    assert crashed is True
    durable = item.store.read()["assetReceipts"]["skill"]
    stage_name = durable.get("restoreStageName")
    quarantine_name = durable.get("quarantineName")
    if expected_state == "stage-created":
        assert isinstance(stage_name, str) and os.path.lexists(skill.parent / stage_name)
        assert os.path.lexists(skill)
    elif expected_state == "target-quarantined":
        assert isinstance(stage_name, str) and os.path.lexists(skill.parent / stage_name)
        assert isinstance(quarantine_name, str)
        assert os.path.lexists(skill.parent / quarantine_name)
        assert not os.path.lexists(skill)
    elif expected_state == "restore-published":
        assert os.path.lexists(skill)
        assert isinstance(quarantine_name, str)
        assert os.path.lexists(skill.parent / quarantine_name)
        assert isinstance(stage_name, str) and not os.path.lexists(skill.parent / stage_name)
    else:
        assert os.path.lexists(skill)
        assert isinstance(quarantine_name, str)
        assert not os.path.lexists(skill.parent / quarantine_name)

    resumed = manager(tmp_path)
    _stub_non_asset_rollback(resumed, monkeypatch)
    resume_transaction = resumed.store.read()
    prepared = resumed._preflight_rollback_assets(resume_transaction, snapshot_dir)
    assert "skill" in prepared

    result = resumed._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    assert result["outcome"] == "restored_exactly"
    assert _asset_state(skill) == expected
    assert resumed.store.read()["assetReceipts"]["skill"]["rollbackComplete"] is True
    _assert_no_recovery_residue(skill.parent)
