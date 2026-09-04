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


ALLOWED_LINK = Path(
    "assets/knowledge-lancedb-template/node_modules/.bin/arrow2csv"
)
ALLOWED_TARGET = "../apache-arrow/bin/arrow2csv.js"


def _skill_target(item: core.IntegrationManager) -> Path:
    return item.paths.workspace / "skills" / core.SKILL_ID


def _write_skill_with_link(
    target: Path,
    *,
    relative: Path = ALLOWED_LINK,
    link_target: str = ALLOWED_TARGET,
) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text("legacy-skill", encoding="utf-8")
    executable = target / (
        "assets/knowledge-lancedb-template/node_modules/apache-arrow/bin/arrow2csv.js"
    )
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    link = target / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(link_target)
    return link


def _snapshot_transaction(
    item: core.IntegrationManager,
) -> tuple[dict[str, Any], Path]:
    transaction = _write_precise_runtime_transaction(item)
    snapshot_dir = Path(str(transaction["configBackupPath"])).parent
    transaction.update(item._snapshot_other_assets(snapshot_dir))
    item.store.write(transaction)
    return transaction, snapshot_dir


def test_skill_snapshot_and_rollback_preserve_exact_npm_bin_link(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    target = _skill_target(item)
    link = _write_skill_with_link(target)
    transaction, snapshot_dir = _snapshot_transaction(item)
    backup_link = snapshot_dir / "skill.preinstall" / ALLOWED_LINK
    assert backup_link.is_symlink()
    assert os.readlink(backup_link) == ALLOWED_TARGET

    item._checkpoint_asset_mutation(transaction, ["skill"])
    (target / "SKILL.md").write_text("upgraded-skill", encoding="utf-8")
    (target / "added-by-upgrade.txt").write_text("remove-me", encoding="utf-8")
    item._capture_asset_post_identity(transaction, "skill")
    durable = item.store.read()
    prepared = item._preflight_rollback_assets(durable, snapshot_dir)
    spec, receipt = prepared["skill"]
    item._rollback_one_asset(durable, spec, receipt, snapshot_dir)

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "legacy-skill"
    assert not (target / "added-by-upgrade.txt").exists()
    assert link.is_symlink()
    assert os.readlink(link) == ALLOWED_TARGET


@pytest.mark.parametrize(
    ("relative", "link_target", "message"),
    [
        (
            ALLOWED_LINK,
            "../../../../../../outside/private.txt",
            "unsafe symbolic link target",
        ),
        (
            ALLOWED_LINK,
            "../apache-arrow/bin/arrow2json.js",
            "unsafe symbolic link target",
        ),
        (
            ALLOWED_LINK.with_name("arrow2json"),
            ALLOWED_TARGET,
            "unsupported symbolic link",
        ),
    ],
)
def test_skill_snapshot_rejects_non_allowlisted_npm_links_without_following(
    tmp_path: Path,
    relative: Path,
    link_target: str,
    message: str,
) -> None:
    item = manager(tmp_path)
    target = _skill_target(item)
    _write_skill_with_link(target, relative=relative, link_target=link_target)
    outside = item.paths.workspace / "outside/private.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("must-not-read-or-copy", encoding="utf-8")
    snapshot_dir = item.paths.state_root / "snapshots/run-skill-link-negative"
    snapshot_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match=message):
        item._snapshot_other_assets(snapshot_dir)

    assert outside.read_text(encoding="utf-8") == "must-not-read-or-copy"
    assert not (snapshot_dir / "skill.preinstall").exists()


def test_skill_post_install_traversal_link_is_rejected_before_rollback_mutation(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    target = _skill_target(item)
    link = _write_skill_with_link(target)
    transaction, snapshot_dir = _snapshot_transaction(item)
    item._checkpoint_asset_mutation(transaction, ["skill"])
    link.unlink()
    link.symlink_to("../../../../../../outside/private.txt")

    with pytest.raises(RuntimeError, match="unsafe symbolic link target"):
        item._capture_asset_post_identity(transaction, "skill")
    durable = item.store.read()
    assert "postSha256" not in durable["assetReceipts"]["skill"]
    with pytest.raises(RuntimeError, match="unsafe symbolic link target"):
        item._preflight_rollback_assets(durable, snapshot_dir)
    assert link.is_symlink()
    assert os.readlink(link) == "../../../../../../outside/private.txt"


def test_skill_backup_sibling_link_tamper_is_rejected_before_rollback_mutation(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    target = _skill_target(item)
    _write_skill_with_link(target)
    transaction, snapshot_dir = _snapshot_transaction(item)
    item._checkpoint_asset_mutation(transaction, ["skill"])
    (target / "SKILL.md").write_text("upgraded-skill", encoding="utf-8")
    item._capture_asset_post_identity(transaction, "skill")
    durable = item.store.read()
    backup_link = snapshot_dir / "skill.preinstall" / ALLOWED_LINK
    backup_link.unlink()
    backup_link.symlink_to("../apache-arrow/bin/arrow2json.js")

    with pytest.raises(RuntimeError, match="backup for skill is unsafe"):
        item._preflight_rollback_assets(durable, snapshot_dir)

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "upgraded-skill"
    assert os.readlink(backup_link) == "../apache-arrow/bin/arrow2json.js"
