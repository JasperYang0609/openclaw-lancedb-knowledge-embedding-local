from __future__ import annotations

import json
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.openclaw_integration.core as integration_core

from src.openclaw_integration.core import (
    CRON_DECLARATION_KEY,
    PLUGIN_ID,
    IntegrationPaths,
    TransactionStore,
    build_cron_add_args,
    merge_allowlist,
    resolve_tool_allowlist_update,
    owned_gemini_jobs,
    IntegrationManager,
)
from src.openclaw_integration.launchd import build_launchd_plist


def paths(tmp_path: Path) -> IntegrationPaths:
    home = tmp_path / "home"
    workspace = home / ".openclaw/workspace"
    project = workspace / "knowledge-lancedb-qwen-local"
    runtime = home / "Library/Application Support/OpenClaw/qwen-local"
    state = home / "Library/Application Support/OpenClaw/qwen-local-integration"
    plist = home / "Library/LaunchAgents/ai.openclaw.qwen-local-embedding.plist"
    for directory in (workspace, project, runtime, state, plist.parent):
        directory.mkdir(parents=True, exist_ok=True)
    return IntegrationPaths(home=home, workspace=workspace, project_root=project, runtime_root=runtime,
                            state_root=state, launchd_plist=plist)


def manager_with_config_result(tmp_path: Path, payload: object) -> tuple[IntegrationManager, Path, list[list[str]]]:
    item = paths(tmp_path)
    config = item.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}")
    config.chmod(0o600)
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    cli = integration_core.OpenClawCli(tmp_path / "openclaw", runner=runner)
    manager = IntegrationManager(paths=item, repo_root=Path(__file__).resolve().parents[1], cli=cli,
                                 node_path=Path(sys.executable))
    return manager, config, calls


def snapshot_run(manager: IntegrationManager) -> Path:
    run_dir = manager.paths.state_root / "snapshots" / "run-00000000-0000-4000-8000-000000000001"
    run_dir.mkdir(parents=True, mode=0o700)
    marker = run_dir / integration_core.SNAPSHOT_MARKER_NAME
    marker.write_text("fixture-run-marker")
    marker.chmod(0o600)
    return run_dir


def restore_kwargs(source: Path) -> dict[str, object]:
    metadata = source.parent.stat()
    return {
        "expected_sha256": integration_core.sha256_file(source),
        "expected_run_identity": (metadata.st_dev, metadata.st_ino),
        "expected_marker_sha256": integration_core.sha256_file(
            source.parent / integration_core.SNAPSHOT_MARKER_NAME
        ),
    }


def test_config_file_uses_official_validate_json_path(tmp_path: Path) -> None:
    manager, config, calls = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr="migration notice\n"
        )

    manager.cli.runner = runner

    assert manager._config_file() == config
    assert calls == [manager.cli.command(["config", "validate", "--json"])]


def test_integration_lock_rejects_concurrent_transaction(tmp_path: Path) -> None:
    manager, _, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})

    with manager._integration_lock():
        with pytest.raises(RuntimeError, match="transaction is active"):
            with manager._integration_lock():
                pass


def test_integration_lock_preserves_consumer_oserror(tmp_path: Path) -> None:
    manager, _, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})

    with pytest.raises(OSError, match="consumer failure"):
        with manager._integration_lock():
            raise OSError("consumer failure")


@pytest.mark.parametrize("payload", [
    {"valid": False, "path": "/tmp/openclaw.json"},
    {"valid": True},
    {"valid": True, "path": ""},
    ["not", "an", "object"],
])
def test_config_file_rejects_untrusted_validate_json_schema(tmp_path: Path, payload: object) -> None:
    manager, _, _ = manager_with_config_result(tmp_path, payload)
    with pytest.raises(RuntimeError, match="validation JSON"):
        manager._config_file()


def test_config_file_rejects_broad_permissions_and_symlink(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    config.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions"):
        manager._config_file()

    config.chmod(0o600)
    link = config.with_name("linked-openclaw.json")
    link.symlink_to(config)
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(link)}), stderr=""
    )
    with pytest.raises((RuntimeError, ValueError), match="symbolic"):
        manager._config_file()


def test_config_file_rejects_hardlink_and_outside_home(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    hardlink = config.with_name("hardlinked-openclaw.json")
    os.link(config, hardlink)
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    with pytest.raises(RuntimeError, match="ownership"):
        manager._config_file()

    outside = tmp_path / "outside-openclaw.json"
    outside.write_text("{}")
    outside.chmod(0o600)
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(outside)}), stderr=""
    )
    with pytest.raises(ValueError, match="managed root"):
        manager._config_file()


def test_config_file_rejects_malformed_json_and_cli_failure(tmp_path: Path) -> None:
    manager, _, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout="notice before json\n{\"valid\":true}", stderr=""
    )
    with pytest.raises(RuntimeError, match="invalid JSON"):
        manager._config_file()

    def failed_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(2, argv, output="", stderr="validation failed")

    manager.cli.runner = failed_runner
    with pytest.raises(subprocess.CalledProcessError):
        manager._config_file()


def test_config_file_rejects_unsafe_parent_and_special_file(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    config.parent.chmod(0o777)
    with pytest.raises(RuntimeError, match="parent permissions"):
        manager._config_file()

    config.parent.chmod(0o700)
    config.unlink()
    os.mkfifo(config, mode=0o600)
    with pytest.raises(RuntimeError, match="ownership"):
        manager._config_file()


def test_unsafe_parent_validation_does_not_leak_directory_fds(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    config.parent.chmod(0o777)
    fd_root = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
    before = len(list(fd_root.iterdir()))
    for _ in range(20):
        with pytest.raises(RuntimeError, match="parent permissions"):
            manager._config_file()
    assert len(list(fd_root.iterdir())) == before


def test_config_reader_preserves_consumer_io_error(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    with pytest.raises(OSError, match="consumer read failure"):
        with manager._open_config_file(config):
            raise OSError("consumer read failure")


def test_config_metadata_rejects_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    metadata = config.lstat()
    monkeypatch.setattr(integration_core.os, "getuid", lambda: metadata.st_uid + 1)
    with pytest.raises(RuntimeError, match="file ownership"):
        manager._validate_private_config(metadata)
    with pytest.raises(RuntimeError, match="parent ownership"):
        manager._validate_private_directory(config.parent.lstat())


def test_config_file_rejects_parent_symlink_and_open_race(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    item = IntegrationPaths(
        home=home,
        workspace=home / ".openclaw/workspace",
        project_root=home / ".openclaw/workspace/knowledge-lancedb-qwen-local",
        runtime_root=home / "Library/Application Support/OpenClaw/qwen-local",
        state_root=home / "Library/Application Support/OpenClaw/qwen-local-integration",
        launchd_plist=home / "Library/LaunchAgents/ai.openclaw.qwen-local-embedding.plist",
    )
    outside_dir = tmp_path / "outside-config"
    outside_dir.mkdir(mode=0o700)
    outside_config = outside_dir / "openclaw.json"
    outside_config.write_text("{}")
    outside_config.chmod(0o600)
    linked_parent = item.home / ".openclaw"
    linked_parent.symlink_to(outside_dir, target_is_directory=True)
    manager, _, _ = manager_with_config_result(tmp_path / "separate", {"valid": True, "path": "placeholder"})
    manager.paths = item
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(linked_parent / "openclaw.json")}), stderr=""
    )
    with pytest.raises(ValueError, match="symbolic"):
        manager._config_file()

    linked_parent.unlink()
    linked_parent.mkdir(mode=0o700)
    config = linked_parent / "openclaw.json"
    config.write_text("{}")
    config.chmod(0o600)
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    real_open = os.open
    replaced = False

    def racing_open(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal replaced
        if path == "openclaw.json" and dir_fd is not None and not replaced:
            replaced = True
            config.unlink()
            config.symlink_to(outside_config)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_core.os, "open", racing_open)
    with pytest.raises(RuntimeError, match="missing or unsafe"):
        manager._config_file()


def test_config_hash_rejects_mid_read_metadata_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    real_fstat = os.fstat
    regular_calls = 0

    def changing_fstat(fd: int) -> object:
        nonlocal regular_calls
        metadata = real_fstat(fd)
        if metadata.st_ino == config.lstat().st_ino:
            regular_calls += 1
            if regular_calls == 3:
                fields = {name: getattr(metadata, name) for name in (
                    "st_mode", "st_nlink", "st_uid", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"
                )}
                fields["st_mtime_ns"] += 1
                return SimpleNamespace(**fields)
        return metadata

    monkeypatch.setattr(integration_core.os, "fstat", changing_fstat)
    with pytest.raises(RuntimeError, match="changed while"):
        manager._sha256_config(config)


def test_snapshot_rejects_symlink_destination(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    outside = tmp_path / "outside-snapshots"
    outside.mkdir(mode=0o700)
    snapshots = manager.paths.state_root / "snapshots"
    snapshots.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="Managed directory"):
        manager.snapshot()
    assert not (outside / "openclaw-config.preinstall").exists()


def test_snapshot_uses_secure_descriptor_and_matching_hash(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    config.write_text('{"safe":true}')
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )

    result = manager.snapshot()

    backup = Path(result["configBackupPath"])
    assert backup.read_text() == config.read_text()
    assert result["preConfigSha256"] == integration_core.sha256_file(backup)
    assert (result["snapshotRunDev"], result["snapshotRunIno"]) == (
        backup.parent.stat().st_dev, backup.parent.stat().st_ino,
    )
    assert backup.stat().st_mode & 0o077 == 0


def test_snapshot_source_open_failure_closes_fd_and_can_retry(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    original = manager._open_config_file
    fd_root = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
    before = len(list(fd_root.iterdir()))

    @contextmanager
    def failing_open(_: Path):
        raise RuntimeError("simulated source race")
        yield

    manager._open_config_file = failing_open
    with pytest.raises(RuntimeError, match="simulated source race"):
        manager.snapshot()
    after = len(list(fd_root.iterdir()))
    assert after == before
    snapshot_dir = manager.paths.state_root / "snapshots"
    assert not snapshot_dir.exists() or list(snapshot_dir.iterdir()) == []

    manager._open_config_file = original
    assert Path(manager.snapshot()["configBackupPath"]).is_file()


def test_snapshot_mid_read_failure_removes_partial_temp(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    original = manager._assert_stable_file

    calls = 0

    def changed(*args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("changed while it was being read")
        original(*args)

    manager._assert_stable_file = changed
    with pytest.raises(RuntimeError, match="changed while"):
        manager.snapshot()
    snapshot_dir = manager.paths.state_root / "snapshots"
    assert list(snapshot_dir.iterdir()) == []

    manager._assert_stable_file = original
    assert Path(manager.snapshot()["configBackupPath"]).is_file()


def test_snapshot_marker_creation_failure_removes_orphan_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    real_open = integration_core.os.open

    def fail_marker_open(path: object, *args: object, **kwargs: object) -> int:
        if path == integration_core.SNAPSHOT_MARKER_NAME:
            raise OSError("simulated marker failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(integration_core.os, "open", fail_marker_open)
    with pytest.raises(OSError, match="simulated marker failure"):
        manager.snapshot()

    snapshot_root = manager.paths.state_root / "snapshots"
    assert snapshot_root.is_dir()
    assert list(snapshot_root.iterdir()) == []


def test_snapshot_later_failure_removes_all_backups_and_can_retry(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    skill_target = manager.paths.workspace / "skills" / integration_core.SKILL_ID
    skill_target.parent.mkdir(parents=True)
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    skill_target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="skill is a symbolic link"):
        manager.snapshot()
    snapshot_dir = manager.paths.state_root / "snapshots"
    assert list(snapshot_dir.iterdir()) == []

    skill_target.unlink()
    assert Path(manager.snapshot()["configBackupPath"]).is_file()


def test_snapshot_failure_preserves_foreign_run_directory(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    snapshot_root = manager.paths.state_root / "snapshots"
    foreign = snapshot_root / "run-00000000-0000-4000-8000-000000000099"
    foreign.mkdir(parents=True, mode=0o700)
    marker = foreign / "foreign-data"
    marker.write_text("preserve")
    skill_target = manager.paths.workspace / "skills" / integration_core.SKILL_ID
    skill_target.parent.mkdir(parents=True)
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    skill_target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="skill is a symbolic link"):
        manager.snapshot()

    assert marker.read_text() == "preserve"
    assert list(snapshot_root.iterdir()) == [foreign]


def test_snapshot_partial_backup_cleanup_preserves_foreign_run(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    snapshot_root = manager.paths.state_root / "snapshots"
    foreign = snapshot_root / "run-00000000-0000-4000-8000-000000000099"
    foreign.mkdir(parents=True, mode=0o700)
    marker = foreign / "foreign-data"
    marker.write_text("preserve")
    skill_target = manager.paths.workspace / "skills" / integration_core.SKILL_ID
    (skill_target / "nested").mkdir(parents=True)
    (skill_target / "nested/content.txt").write_text("existing skill")
    outside_plist = tmp_path / "outside.plist"
    outside_plist.write_text("foreign")
    manager.paths.launchd_plist.symlink_to(outside_plist)

    with pytest.raises(RuntimeError, match="plist is a symbolic link"):
        manager.snapshot()

    assert marker.read_text() == "preserve"
    assert list(snapshot_root.iterdir()) == [foreign]


def test_recorded_snapshot_cleanup_removes_only_manifest_run(tmp_path: Path) -> None:
    manager, _, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    recorded = snapshot_run(manager)
    backup = recorded / "openclaw-config.preinstall"
    backup.write_text("recorded")
    backup.chmod(0o600)
    foreign = manager.paths.state_root / "snapshots" / "run-00000000-0000-4000-8000-000000000099"
    foreign.mkdir(mode=0o700)
    marker = foreign / "foreign-data"
    marker.write_text("preserve")
    recorded_metadata = recorded.stat()
    run_marker_sha256 = integration_core.sha256_file(recorded / integration_core.SNAPSHOT_MARKER_NAME)

    assert manager._remove_recorded_snapshot_run(
        backup, (recorded_metadata.st_dev, recorded_metadata.st_ino), run_marker_sha256,
    ) is True

    assert not recorded.exists()
    assert marker.read_text() == "preserve"
    assert manager._remove_recorded_snapshot_run(
        backup, (recorded_metadata.st_dev, recorded_metadata.st_ino), run_marker_sha256,
    ) is False


def test_recorded_snapshot_cleanup_rejects_same_path_replacement(tmp_path: Path) -> None:
    manager, _, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    recorded = snapshot_run(manager)
    backup = recorded / "openclaw-config.preinstall"
    backup.write_text("recorded")
    backup.chmod(0o600)
    original = recorded.stat()
    marker_sha256 = integration_core.sha256_file(recorded / integration_core.SNAPSHOT_MARKER_NAME)
    shutil.rmtree(recorded)
    recorded.mkdir(mode=0o700)
    marker = recorded / "foreign-data"
    marker.write_text("preserve")

    with pytest.raises(RuntimeError, match="changed before cleanup|marker"):
        manager._remove_recorded_snapshot_run(
            backup, (original.st_dev, original.st_ino), marker_sha256,
        )

    assert marker.read_text() == "preserve"


def test_recorded_snapshot_cleanup_rejects_changed_marker_on_same_directory(tmp_path: Path) -> None:
    manager, _, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    recorded = snapshot_run(manager)
    backup = recorded / "openclaw-config.preinstall"
    backup.write_text("recorded")
    backup.chmod(0o600)
    identity = recorded.stat()
    marker = recorded / integration_core.SNAPSHOT_MARKER_NAME
    expected_marker_sha256 = integration_core.sha256_file(marker)
    marker.write_text("replacement-marker")
    marker.chmod(0o600)

    with pytest.raises(RuntimeError, match="marker mismatch"):
        manager._remove_recorded_snapshot_run(
            backup, (identity.st_dev, identity.st_ino), expected_marker_sha256,
        )

    assert backup.read_text() == "recorded"
    assert marker.read_text() == "replacement-marker"


def test_restore_config_rejects_symlinked_target_parent(tmp_path: Path) -> None:
    manager, _, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    snapshot_dir = snapshot_run(manager)
    source = snapshot_dir / "openclaw-config.preinstall"
    source.write_text("original")
    source.chmod(0o600)
    outside = tmp_path / "outside-target"
    outside.mkdir(mode=0o700)
    outside_config = outside / "custom.json"
    outside_config.write_text("outside")
    outside_config.chmod(0o600)
    linked_parent = manager.paths.home / "linked-config"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="Managed directory"):
        manager._restore_config_file(source, linked_parent / "custom.json", **restore_kwargs(source))
    assert outside_config.read_text() == "outside"


def test_restore_config_rejects_symlinked_source_parent(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    outside = tmp_path / "outside-source"
    outside.mkdir(mode=0o700)
    outside_source = outside / "openclaw-config.preinstall"
    outside_source.write_text("outside")
    outside_source.chmod(0o600)
    outside_marker = outside / integration_core.SNAPSHOT_MARKER_NAME
    outside_marker.write_text("outside-marker")
    outside_marker.chmod(0o600)
    snapshot_root = manager.paths.state_root / "snapshots"
    snapshot_root.symlink_to(outside, target_is_directory=True)
    snapshot_dir = snapshot_root / "run-00000000-0000-4000-8000-000000000001"

    with pytest.raises(RuntimeError, match="Managed directory"):
        manager._restore_config_file(
            snapshot_dir / "openclaw-config.preinstall", config, **restore_kwargs(outside_source)
        )
    assert config.read_text() == "{}"


def test_restore_config_atomically_replaces_custom_config_name(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    snapshot_dir = snapshot_run(manager)
    source = snapshot_dir / "openclaw-config.preinstall"
    source.write_text("original")
    source.chmod(0o600)
    custom = config.with_name("profile-config.json")
    custom.write_text("changed")
    custom.chmod(0o600)

    manager._restore_config_file(source, custom, **restore_kwargs(source))

    assert custom.read_text() == "original"
    assert custom.stat().st_mode & 0o077 == 0
    assert not custom.with_name("profile-config.json.restore-tmp").exists()


def test_restore_config_integrity_failure_preserves_target_and_cleans_temp(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    snapshot_dir = snapshot_run(manager)
    source = snapshot_dir / "openclaw-config.preinstall"
    source.write_text("original")
    source.chmod(0o600)

    def fail_integrity(*_: object) -> None:
        raise RuntimeError("simulated integrity failure")

    manager._assert_stable_file = fail_integrity
    with pytest.raises(RuntimeError, match="simulated integrity failure"):
        manager._restore_config_file(source, config, **restore_kwargs(source))
    assert config.read_text() == "{}"
    assert not config.with_name("openclaw.json.restore-tmp").exists()


def test_restore_config_rejects_tampered_snapshot_hash(tmp_path: Path) -> None:
    manager, config, _ = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    snapshot_dir = snapshot_run(manager)
    source = snapshot_dir / "openclaw-config.preinstall"
    source.write_text("tampered")
    source.chmod(0o600)
    identity = snapshot_dir.stat()

    with pytest.raises(RuntimeError, match="hash mismatch"):
        manager._restore_config_file(
            source, config,
            expected_sha256=hashlib.sha256(b"original").hexdigest(),
            expected_run_identity=(identity.st_dev, identity.st_ino),
            expected_marker_sha256=integration_core.sha256_file(
                snapshot_dir / integration_core.SNAPSHOT_MARKER_NAME
            ),
        )

    assert config.read_text() == "{}"
    assert not config.with_name("openclaw.json.restore-tmp").exists()


def test_rollback_preflights_snapshot_before_any_side_effect(tmp_path: Path) -> None:
    manager, config, calls = manager_with_config_result(tmp_path, {"valid": True, "path": "placeholder"})
    manager.cli.runner = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"valid": True, "path": str(config)}), stderr=""
    )
    snapshot = manager.snapshot()
    manager.store.write({
        "schemaVersion": 1,
        "runId": "fixture-run",
        "phase": "failed",
        "ownedAssets": [],
        "projectCreated": False,
        **snapshot,
    })
    backup = Path(snapshot["configBackupPath"])
    backup.write_text("tampered")
    backup.chmod(0o600)
    side_effects: list[str] = []
    manager.deactivate_launchd = lambda: side_effects.append("deactivate")

    with pytest.raises(RuntimeError, match="hash mismatch"):
        manager._rollback_locked(require_exact_post_config=False)

    assert side_effects == []
    assert calls == []


def test_paths_reject_wrong_project_identity_and_symlink(tmp_path: Path) -> None:
    item = paths(tmp_path)
    item.validate()
    with pytest.raises(ValueError, match="Qwen project"):
        IntegrationPaths(home=item.home, workspace=item.workspace, project_root=item.workspace / "knowledge-lancedb",
                         runtime_root=item.runtime_root, state_root=item.state_root,
                         launchd_plist=item.launchd_plist).validate()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = item.workspace / "knowledge-lancedb-qwen-local"
    linked.rmdir()
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        IntegrationPaths(home=item.home, workspace=item.workspace, project_root=linked,
                         runtime_root=item.runtime_root, state_root=item.state_root,
                         launchd_plist=item.launchd_plist).validate()


def test_transaction_store_is_restricted_atomic_and_rejects_secrets(tmp_path: Path) -> None:
    item = paths(tmp_path)
    store = TransactionStore(item.state_root)
    manifest = store.write({"schemaVersion": 1, "runId": "run-1", "phase": "prepared", "ownedAssets": []})
    assert manifest.stat().st_mode & 0o077 == 0
    assert store.read()["runId"] == "run-1"
    with pytest.raises(ValueError, match="forbidden"):
        store.write({"schemaVersion": 1, "runId": "run-2", "token": "do-not-store"})


def test_launchd_plist_uses_fixed_argv_loopback_and_no_shell(tmp_path: Path) -> None:
    item = paths(tmp_path)
    server = item.runtime_root / "runtime/llama-server"
    model = item.runtime_root / "models/model.gguf"
    key = item.runtime_root / "run/api-key"
    for file_path in (server, model, key):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("fixture")
    payload = build_launchd_plist(server=server, model=model, api_key_file=key, port=18889,
                                  stdout_path=item.state_root / "logs/server.out.log",
                                  stderr_path=item.state_root / "logs/server.err.log")
    parsed = plistlib.loads(payload)
    argv = parsed["ProgramArguments"]
    assert argv[0] == str(server)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "18889"
    assert "--api-key-file" in argv
    assert "/bin/sh" not in argv and "bash" not in argv
    assert parsed["RunAtLoad"] is True


def test_cron_contract_is_idempotent_fixed_argv_and_output_bounded(tmp_path: Path) -> None:
    item = paths(tmp_path)
    node = tmp_path / "node"
    script = item.project_root / "scripts/knowledge_index_incremental.sh"
    node.write_text("fixture")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("fixture")
    args = build_cron_add_args(project_root=item.project_root, incremental_script=script)
    assert args[args.index("--declaration-key") + 1] == CRON_DECLARATION_KEY
    assert args[args.index("--cron") + 1] == "30 6 * * *"
    assert json.loads(args[args.index("--command-argv") + 1]) == [str(script)]
    assert args[args.index("--command-cwd") + 1] == str(item.project_root)
    assert int(args[args.index("--output-max-bytes") + 1]) <= 65536
    assert "--command" not in args


def test_job_argv_supports_current_and_legacy_cron_schemas() -> None:
    current = {"payload": {"argv": ["/safe/current.sh"]}}
    legacy = {"payload": {"command": {"argv": ["/safe/legacy.sh"]}}}

    assert integration_core._job_argv(current) == ["/safe/current.sh"]
    assert integration_core._job_argv(legacy) == ["/safe/legacy.sh"]


class VerificationCli:
    def __init__(self, jobs: list[dict[str, object]]) -> None:
        self.jobs = jobs

    def json(self, args: list[str], *, timeout: int = 120) -> object:
        if args[:2] == ["plugins", "inspect"]:
            return {"id": PLUGIN_ID, "tools": [integration_core.TOOL_NAME]}
        if args[:2] == ["skills", "info"]:
            return {"id": integration_core.SKILL_ID, "eligible": True}
        if args[:3] == ["cron", "list", "--all"]:
            return {"jobs": self.jobs}
        if args[:2] == ["gateway", "status"]:
            return {"ok": True}
        raise AssertionError(f"unexpected verification command: {args}")


def verification_manager(
    tmp_path: Path, jobs: list[dict[str, object]]
) -> tuple[IntegrationManager, Path]:
    item = paths(tmp_path)
    incremental = item.project_root / "scripts/knowledge_index_incremental.sh"
    incremental.parent.mkdir(parents=True, exist_ok=True)
    incremental.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = IntegrationManager(
        paths=item,
        repo_root=Path(__file__).resolve().parents[1],
        cli=VerificationCli(jobs),
        node_path=Path(sys.executable),
    )
    manager.store.write(
        {"schemaVersion": 1, "runId": "verify", "phase": "committed", "ownedAssets": []}
    )
    return manager, incremental


@pytest.mark.parametrize("legacy_schema", [False, True])
def test_verify_accepts_one_managed_job_in_each_cron_schema(
    tmp_path: Path, legacy_schema: bool
) -> None:
    item = paths(tmp_path)
    script = item.project_root / "scripts/knowledge_index_incremental.sh"
    argv = [str(script)]
    payload = {"command": {"argv": argv}} if legacy_schema else {"argv": argv}
    jobs: list[dict[str, object]] = [
        {
            "id": "canonical",
            "enabled": True,
            "declarationKey": CRON_DECLARATION_KEY,
            "payload": payload,
        }
    ]
    manager, _ = verification_manager(tmp_path, jobs)

    assert manager.verify()["incrementalCronUnique"] is True


def test_verify_rejects_duplicate_enabled_managed_incremental_jobs(tmp_path: Path) -> None:
    item = paths(tmp_path)
    script = item.project_root / "scripts/knowledge_index_incremental.sh"
    jobs: list[dict[str, object]] = [
        {
            "id": "canonical",
            "enabled": True,
            "declarationKey": CRON_DECLARATION_KEY,
            "payload": {"argv": [str(script)]},
        },
        {
            "id": "legacy-duplicate",
            "enabled": True,
            "payload": {"command": {"argv": ["sh", "-lc", str(script)]}},
        },
    ]
    manager, _ = verification_manager(tmp_path, jobs)

    with pytest.raises(RuntimeError, match="command is missing or duplicated"):
        manager.verify()


def test_verify_ignores_disabled_managed_incremental_duplicate(tmp_path: Path) -> None:
    item = paths(tmp_path)
    script = item.project_root / "scripts/knowledge_index_incremental.sh"
    jobs: list[dict[str, object]] = [
        {
            "id": "canonical",
            "enabled": True,
            "declarationKey": CRON_DECLARATION_KEY,
            "payload": {"argv": [str(script)]},
        },
        {
            "id": "disabled-duplicate",
            "enabled": False,
            "payload": {"command": {"argv": ["sh", "-lc", str(script)]}},
        },
    ]
    manager, _ = verification_manager(tmp_path, jobs)

    assert manager.verify()["incrementalCronUnique"] is True


def test_verify_does_not_parse_arbitrary_shell_command_text(tmp_path: Path) -> None:
    item = paths(tmp_path)
    script = item.project_root / "scripts/knowledge_index_incremental.sh"
    jobs: list[dict[str, object]] = [
        {
            "id": "canonical",
            "enabled": True,
            "declarationKey": CRON_DECLARATION_KEY,
            "payload": {"argv": [str(script)]},
        },
        {
            "id": "different-command",
            "enabled": True,
            "payload": {"argv": ["sh", "-lc", f"{script} --unexpected"]},
        },
    ]
    manager, _ = verification_manager(tmp_path, jobs)

    assert manager.verify()["incrementalCronUnique"] is True


def test_allowlists_merge_without_removing_existing_entries() -> None:
    assert merge_allowlist(["message", "status_update_ui"], PLUGIN_ID) == [
        "message", "status_update_ui", PLUGIN_ID,
    ]
    assert merge_allowlist(None, PLUGIN_ID) is None
    assert merge_allowlist(None, PLUGIN_ID, create_if_missing=True) == [PLUGIN_ID]


def test_tool_allowlist_update_prefers_exact_allowlist() -> None:
    path_name, values = resolve_tool_allowlist_update(
        ["message"], ["status_update_ui"], "local_knowledge_search"
    )
    assert path_name == "tools.allow"
    assert values == ["message", "local_knowledge_search"]


def test_tool_allowlist_update_uses_also_allow_when_exact_allowlist_is_absent() -> None:
    path_name, values = resolve_tool_allowlist_update(
        None, ["message", "status_update_ui"], "local_knowledge_search"
    )
    assert path_name == "tools.alsoAllow"
    assert values == ["message", "status_update_ui", "local_knowledge_search"]


def test_tool_allowlist_update_preserves_unrestricted_configuration() -> None:
    assert resolve_tool_allowlist_update(None, None, "local_knowledge_search") == (None, None)


def test_tool_allowlist_update_rejects_unexpected_schema() -> None:
    with pytest.raises(RuntimeError, match="unexpected schema"):
        resolve_tool_allowlist_update(None, "message", "local_knowledge_search")


def test_only_exact_owned_gemini_jobs_are_selected() -> None:
    jobs = [
        {"id": "owned", "declarationKey": "openclaw-lancedb-knowledge-gemini-incremental-v1",
         "payload": {"command": {"argv": ["/safe/knowledge-lancedb/scripts/knowledge_index_incremental.sh"]}}},
        {"id": "owned-current", "declarationKey": "openclaw-lancedb-knowledge-gemini-incremental-v1",
         "payload": {"argv": ["/safe/knowledge-lancedb/scripts/knowledge_index_incremental.sh"]}},
        {"id": "name-only", "name": "Gemini knowledge incremental"},
        {"id": "wrong-command", "declarationKey": "openclaw-lancedb-knowledge-gemini-incremental-v1",
         "payload": {"command": {"argv": ["/tmp/attacker.sh"]}}},
    ]
    assert [job["id"] for job in owned_gemini_jobs(jobs)] == ["owned", "owned-current"]


def test_runtime_sync_preserves_config_data_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = paths(tmp_path)
    for relative, content in (("config/source-map.json", "{\"keep\":true}"),
                              ("data/existing.index", "keep-index"), ("reports/existing.json", "keep-report"),
                              ("src/old.js", "old"), ("scripts/old.sh", "old"),
                              ("package.json", "{}"), ("package-lock.json", "{}")):
        target = item.project_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    fake_npm = tmp_path / "npm"
    fake_npm.write_text("fixture")
    fake_npm.chmod(0o700)
    calls = []
    monkeypatch.setattr(integration_core.shutil, "which", lambda name: str(fake_npm) if name == "npm" else None)
    monkeypatch.setattr(integration_core.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    manager = IntegrationManager(paths=item, repo_root=Path(__file__).resolve().parents[1], cli=None,
                                 node_path=Path(sys.executable))

    manager.synchronize_project_runtime()

    assert (item.project_root / "config/source-map.json").read_text() == "{\"keep\":true}"
    assert (item.project_root / "data/existing.index").read_text() == "keep-index"
    assert (item.project_root / "reports/existing.json").read_text() == "keep-report"
    assert not (item.project_root / "src/old.js").exists()
    assert (item.project_root / "src/cli.js").is_file()
    assert calls[0][0][-2:] == ["ci", "--ignore-scripts"]
    assert calls[0][1]["shell"] is False


def test_plugin_is_packed_as_archive_without_lifecycle_scripts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = paths(tmp_path)
    fake_npm = tmp_path / "npm"
    fake_npm.write_text("fixture")
    fake_npm.chmod(0o700)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        staging = Path(argv[argv.index("--pack-destination") + 1])
        archive = staging / "openclaw-lancedb-knowledge-local-plugin-0.1.0.tgz"
        archive.write_bytes(b"fixture")
        return integration_core.subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([{"filename": archive.name}]), stderr="",
        )

    monkeypatch.setattr(integration_core.shutil, "which", lambda name: str(fake_npm) if name == "npm" else None)
    monkeypatch.setattr(integration_core.subprocess, "run", fake_run)
    manager = IntegrationManager(paths=item, repo_root=Path(__file__).resolve().parents[1], cli=None,
                                 node_path=Path(sys.executable))

    archive = manager.package_plugin_archive()

    assert archive.is_file()
    assert calls[0][0][1:3] == ["pack", "--json"]
    assert "--ignore-scripts" in calls[0][0]
    assert calls[0][1]["shell"] is False
