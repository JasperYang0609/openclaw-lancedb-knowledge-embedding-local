from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import src.openclaw_integration.core as core
from test_openclaw_reconciliation_v2 import (
    _write_precise_runtime_transaction,
    manager,
)


def _target(item: core.IntegrationManager, asset_id: str) -> Path:
    if asset_id == "plugin":
        return item.plugin_target
    if asset_id == "skill":
        return item.paths.workspace / "skills" / core.SKILL_ID
    raise AssertionError(asset_id)


def _write_tree(path: Path, value: str, asset_id: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    filename = "index.js" if asset_id == "plugin" else "SKILL.md"
    (path / filename).write_text(value, encoding="utf-8")


def _transaction(
    item: core.IntegrationManager,
) -> tuple[dict[str, Any], Path]:
    transaction = _write_precise_runtime_transaction(item)
    snapshot_dir = Path(transaction["configBackupPath"]).parent
    transaction.update(item._snapshot_other_assets(snapshot_dir))
    item.store.write(transaction)
    return transaction, snapshot_dir


@pytest.mark.parametrize("asset_id", ["plugin", "skill"])
def test_parent_replacement_before_mutation_is_refused_without_receipt_change(
    tmp_path: Path, asset_id: str,
) -> None:
    item = manager(tmp_path)
    target = _target(item, asset_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    transaction, _snapshot_dir = _transaction(item)
    original_parent = target.parent.with_name(f"{target.parent.name}.original")
    target.parent.rename(original_parent)
    target.parent.mkdir(mode=0o700)
    marker = target.parent / "external-parent.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(RuntimeError, match=f"parent for {asset_id} changed"):
        item._checkpoint_asset_mutation(transaction, [asset_id])

    durable = item.store.read()
    assert durable["assetReceipts"][asset_id]["mutationStarted"] is False
    assert durable[f"{asset_id}MutationStarted"] is False
    assert marker.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("asset_id", ["plugin", "skill"])
def test_parent_replacement_during_install_is_refused_by_capture_and_rollback(
    tmp_path: Path, asset_id: str,
) -> None:
    item = manager(tmp_path)
    target = _target(item, asset_id)
    _write_tree(target, "preinstall", asset_id)
    transaction, snapshot_dir = _transaction(item)
    item._checkpoint_asset_mutation(transaction, [asset_id])

    original_parent = target.parent.with_name(f"{target.parent.name}.original")
    target.parent.rename(original_parent)
    target.parent.mkdir(mode=0o700)
    _write_tree(target, "external-replacement", asset_id)
    marker = target / ("index.js" if asset_id == "plugin" else "SKILL.md")

    with pytest.raises(RuntimeError, match=f"parent for {asset_id} changed"):
        item._capture_asset_post_identity(transaction, asset_id)

    durable = item.store.read()
    receipt = durable["assetReceipts"][asset_id]
    assert "postParentDev" not in receipt
    assert "postParentIno" not in receipt
    with pytest.raises(RuntimeError, match=f"parent for {asset_id} changed"):
        item._preflight_rollback_assets(durable, snapshot_dir)
    assert marker.read_text(encoding="utf-8") == "external-replacement"


@pytest.mark.parametrize("asset_id", ["plugin", "skill"])
def test_fresh_parent_transition_is_checkpointed_before_mutation_and_accepted_by_rollback(
    tmp_path: Path, asset_id: str,
) -> None:
    item = manager(tmp_path)
    target = _target(item, asset_id)
    assert not os.path.lexists(target.parent)
    transaction, snapshot_dir = _transaction(item)

    item._checkpoint_asset_mutation(transaction, [asset_id])
    receipt = transaction["assetReceipts"][asset_id]
    parent = target.parent.stat()
    assert receipt["parentPreExisted"] is False
    assert receipt["parentCreatePlanned"] is True
    assert receipt["parentCreated"] is True
    assert receipt["parentPublished"] is True
    assert (receipt["parentDev"], receipt["parentIno"]) == (
        parent.st_dev,
        parent.st_ino,
    )
    assert receipt["mutationStarted"] is True

    _write_tree(target, "installed", asset_id)
    item._capture_asset_post_identity(transaction, asset_id)
    prepared = item._preflight_rollback_assets(transaction, snapshot_dir)
    spec, durable_receipt = prepared[asset_id]
    item._rollback_one_asset(transaction, spec, durable_receipt, snapshot_dir)

    assert not os.path.lexists(target)
    assert target.parent.is_dir()


@pytest.mark.parametrize("asset_id", ["plugin", "skill"])
def test_rollback_rejects_uncheckpointed_fresh_parent_transition(
    tmp_path: Path, asset_id: str,
) -> None:
    item = manager(tmp_path)
    target = _target(item, asset_id)
    transaction, snapshot_dir = _transaction(item)
    target.parent.mkdir(mode=0o700)
    _write_tree(target, "external", asset_id)
    receipt = transaction["assetReceipts"][asset_id]
    receipt["mutationStarted"] = True
    transaction[f"{asset_id}MutationStarted"] = True
    item.store.write(transaction)

    with pytest.raises(RuntimeError, match="not checkpointed|without ownership"):
        item._preflight_rollback_assets(transaction, snapshot_dir)

    marker = target / ("index.js" if asset_id == "plugin" else "SKILL.md")
    assert marker.read_text(encoding="utf-8") == "external"


def test_checkpoint_asset_receipt_rejects_conflict_atomically(tmp_path: Path) -> None:
    item = manager(tmp_path)
    target = item.paths.workspace / "skills" / core.SKILL_ID
    target.parent.mkdir(parents=True, exist_ok=True)
    transaction, _snapshot_dir = _transaction(item)
    receipt = transaction["assetReceipts"]["skill"]
    before = dict(receipt)

    with pytest.raises(RuntimeError, match="identity attempted to change"):
        item._checkpoint_asset_receipt(transaction, "skill", {
            "newIdentityField": 123,
            "parentDev": int(receipt["parentDev"]) + 1,
        })

    assert receipt == before
