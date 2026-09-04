from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "openclaw-lancedb-knowledge-local/assets/knowledge-lancedb-template/scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


health = load_module("qwen_backup_health_component", SCRIPTS / "backup_health_component.py")
runner = load_module("qwen_verified_snapshot_runner", SCRIPTS / "run_verified_snapshot.py")


@pytest.fixture(autouse=True)
def private_test_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake_home))


def ownership_fixture(tmp_path: Path, *, phase: str = "committed") -> tuple[Path, dict[str, str]]:
    fixture_root = Path.home() / "snapshot-fixtures" / tmp_path.name
    fixture_root.mkdir(parents=True, mode=0o700)
    project = fixture_root / "knowledge-lancedb-qwen-local"
    snapshot_root = fixture_root / "private-snapshots"
    state_root = fixture_root / "integration-state"
    project.mkdir(mode=0o755)
    (project / "data").mkdir(mode=0o755)
    snapshot_root.mkdir(mode=0o700)
    state_root.mkdir(mode=0o700)
    for relative, content in {
        "config/source-map.json": json.dumps({
            "embedding": {"provider": "qwen-local", "endpoint": "http://127.0.0.1:18888"},
            "sources": [],
        }),
        "scripts/snapshot_knowledge_assets.py": "# fixture\n",
    }.items():
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    ownership = {
        "schema": runner.OWNERSHIP_SCHEMA,
        "snapshotContract": runner.SNAPSHOT_CONTRACT,
        "provider": "qwen-local",
        "localOnly": True,
        "projectRoot": str(project),
        "snapshotRoot": str(snapshot_root),
        "healthReceiptPath": str(project / "reports/backup-health-component.qwen-local.json"),
        "snapshotScriptPath": str(project / "scripts/snapshot_knowledge_assets.py"),
        "snapshotWrapperPath": str(project / "scripts/run_verified_snapshot.py"),
        "indexLockPath": str(project / "data/index.lock"),
        "timezone": "Asia/Taipei",
        "tableName": runner.TABLE_NAME,
        "healthReceiptSchema": "backup-health-component.v1",
        "incrementalDeclarationKey": "openclaw-lancedb-knowledge-local-incremental-v1",
        "snapshotDeclarationKey": "openclaw-lancedb-knowledge-local-snapshot-v1",
        "initialDeclarationKey": "openclaw-lancedb-knowledge-local-initial-v1",
    }
    manifest = state_root / "transaction.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, "phase": phase, "ownership": ownership}), encoding="utf-8")
    manifest.chmod(0o600)
    return manifest, ownership


def test_health_receipt_has_consumer_identity_freshness_and_string_data_loss(tmp_path: Path) -> None:
    payload = health.build_receipt(event="snapshot", status="error", anomaly_code="SNAPSHOT_FAILED")

    assert payload["producer"] == "qwen-local"
    assert payload["declarationKey"] == "openclaw-lancedb-knowledge-local-snapshot-v1"
    assert payload["freshness"] == {"status": "current", "maxAgeSeconds": 129600}
    assert payload["anomalies"][0]["dataLoss"] == "unknown"
    assert health.validate_receipt(payload)["schema"] == "backup-health-component.v1"

    payload["anomalies"][0]["dataLoss"] = False
    with pytest.raises(ValueError, match="data-loss"):
        health.validate_receipt(payload)


def test_health_receipt_is_atomic_private_and_bounded(tmp_path: Path) -> None:
    receipt = tmp_path / "reports/receipt.json"
    health.write_receipt(receipt, health.build_receipt(event="incremental", status="ok", rows=42))

    assert receipt.stat().st_mode & 0o077 == 0
    assert receipt.parent.stat().st_mode & 0o077 == 0
    assert receipt.stat().st_size <= health.MAX_RECEIPT_BYTES
    assert json.loads(receipt.read_text(encoding="utf-8"))["metrics"] == {"rows": 42}
    assert not list(receipt.parent.glob(f".{receipt.name}.*"))


def test_health_receipt_rejects_hardlink_and_symlinked_parent(tmp_path: Path) -> None:
    receipt = tmp_path / "reports/receipt.json"
    payload = health.build_receipt(event="incremental", status="ok", rows=42)
    health.write_receipt(receipt, payload)
    os.link(receipt, receipt.with_name("receipt-copy.json"))
    with pytest.raises(RuntimeError, match="unsafe"):
        health.write_receipt(receipt, payload)

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unsafe"):
        health.write_receipt(linked / "receipt.json", payload)
    assert not (outside / "receipt.json").exists()


def test_health_writer_requires_exact_private_ownership_contract(tmp_path: Path) -> None:
    manifest, ownership = ownership_fixture(tmp_path)
    project, receipt = health.ownership_paths(manifest)
    assert project == Path(ownership["projectRoot"])
    assert receipt == Path(ownership["healthReceiptPath"])

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["ownership"]["snapshotDeclarationKey"] = "wrong-key"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsupported"):
        health.ownership_paths(manifest)

    payload["ownership"]["snapshotDeclarationKey"] = health.DECLARATION_KEYS["snapshot"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions"):
        health.ownership_paths(manifest)


def test_ownership_manifest_reader_rejects_oversize_hardlink_and_leaf_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "oversize").mkdir()
    manifest, _ = ownership_fixture(tmp_path / "oversize")
    manifest.write_bytes(b"{" + b" " * health.MAX_INPUT_BYTES + b"}")
    with pytest.raises(RuntimeError, match="size"):
        health.ownership_paths(manifest)

    (tmp_path / "hardlink").mkdir()
    manifest, _ = ownership_fixture(tmp_path / "hardlink")
    os.link(manifest, manifest.with_name("transaction-copy.json"))
    with pytest.raises(RuntimeError, match="ownership"):
        health.ownership_paths(manifest)

    (tmp_path / "swap").mkdir()
    manifest, _ = ownership_fixture(tmp_path / "swap")
    outside = tmp_path / "outside-manifest.json"
    outside.write_text("{}", encoding="utf-8")
    outside.chmod(0o600)
    real_open = health.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == manifest.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            manifest.unlink()
            manifest.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(health.os, "open", swapping_open)
    with pytest.raises(RuntimeError, match="unsafe"):
        health.ownership_paths(manifest)
    assert swapped is True


def test_snapshot_run_lock_serializes_parallel_cli_invocations(tmp_path: Path) -> None:
    manifest, ownership = ownership_fixture(tmp_path)
    root = Path(ownership["snapshotRoot"])

    with runner.snapshot_run_lock(root):
        with pytest.raises(runner.SnapshotBusy):
            with runner.snapshot_run_lock(root):
                pass
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_verified_snapshot.py"),
             "--ownership-manifest", str(manifest)],
            text=True, capture_output=True, check=False,
        )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True, "reason": "snapshot-run-active", "status": "skipped"}
    receipt = json.loads(Path(ownership["healthReceiptPath"]).read_text(encoding="utf-8"))
    assert receipt["status"] == "warning"
    assert receipt["anomalies"] == [{
        "code": "QWEN_SNAPSHOT_RUN_ACTIVE",
        "summary": "Qwen 快照本輪因另一個快照仍在執行而安全跳過",
        "impact": "本輪尚未產生新的已驗證快照",
        "dataLoss": "no",
        "repairStatus": "既有執行完成後由下一輪重新驗證",
    }]
    lock = root / runner.SNAPSHOT_RUN_LOCK
    assert lock.is_file() and lock.stat().st_mode & 0o077 == 0


def test_snapshot_cannot_start_during_index_and_never_removes_live_lock(tmp_path: Path) -> None:
    lock = tmp_path / "index.lock"
    lock.mkdir()

    with pytest.raises(TimeoutError, match="bounded"):
        with runner.snapshot_index_lock(
            lock, wait_seconds=0.0, poll_seconds=0.5, clock=lambda: 0.0,
        ):
            pytest.fail("snapshot body must not start while an index owns the lock")

    assert lock.is_dir()


def test_snapshot_waits_for_index_release_then_atomically_owns_shared_lock(tmp_path: Path) -> None:
    lock = tmp_path / "data/index.lock"
    lock.parent.mkdir()
    lock.mkdir(mode=0o700)
    events: list[str] = []

    def release_index(_: float) -> None:
        assert lock.is_dir()
        events.append("index-released")
        lock.rmdir()

    with runner.snapshot_index_lock(
        lock,
        wait_seconds=1.0,
        poll_seconds=0.1,
        sleeper=release_index,
        clock=lambda: 0.0,
    ):
        events.append("snapshot-entered")
        assert lock.is_dir()

    assert events == ["index-released", "snapshot-entered"]
    assert not lock.exists()


def test_index_cannot_start_during_snapshot_owned_shared_lock(tmp_path: Path) -> None:
    lock = tmp_path / "data/index.lock"
    lock.parent.mkdir(mode=0o755)

    with runner.snapshot_index_lock(
        lock, wait_seconds=0.0, poll_seconds=0.1, clock=lambda: 0.0,
    ):
        assert lock.is_dir()
        with pytest.raises(FileExistsError):
            os.mkdir(lock, 0o700)

    assert not lock.exists()


def test_snapshot_shared_lock_accepts_owner_safe_0755_data_parent_without_chmod(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    lock = data / "index.lock"

    with runner.snapshot_index_lock(
        lock, wait_seconds=0.0, poll_seconds=0.1, clock=lambda: 0.0,
    ):
        assert lock.is_dir()
        assert stat.S_IMODE(data.stat().st_mode) == 0o755

    assert stat.S_IMODE(data.stat().st_mode) == 0o755


def test_snapshot_release_refuses_replacement_lock_without_removing_it(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    lock = data / "index.lock"
    displaced = data / "original-index.lock"

    with pytest.raises(RuntimeError, match="identity changed"):
        with runner.snapshot_index_lock(
            lock, wait_seconds=0.0, poll_seconds=0.1, clock=lambda: 0.0,
        ):
            lock.rename(displaced)
            lock.mkdir(mode=0o700)

    assert displaced.is_dir()
    assert lock.is_dir()


def test_stale_valid_daily_creates_immutable_repair_then_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, ownership = ownership_fixture(tmp_path)
    snapshot_root = Path(ownership["snapshotRoot"])
    daily = snapshot_root / "snapshots/daily-2026-09-04"
    daily.mkdir(parents=True)
    events: list[str] = []

    monkeypatch.setattr(runner, "trusted_closeout", lambda *_: ("2026-09-04T01:00:00+00:00", 7))
    monkeypatch.setattr(runner, "verify_snapshot", lambda _: {"createdAt": "2026-09-04T00:00:00+00:00"})

    def create(_project: Path, root: Path, name: str, _required: list[str]):
        events.append(f"create:{name}")
        (root / "snapshots" / name).mkdir(parents=True)

    monkeypatch.setattr(runner, "create_snapshot", create)
    monkeypatch.setattr(runner, "verify_complete", lambda *args: events.append(f"verify:{args[1].name}"))
    monkeypatch.setattr(runner, "prune_daily_snapshots", lambda *_: events.append("prune-daily"))
    monkeypatch.setattr(runner, "prune_transient_snapshots", lambda *_: events.append("prune-transient"))
    monkeypatch.setattr(runner, "write_receipt", lambda *_: events.append("receipt"))

    result = runner.run_snapshot(
        manifest, now=datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
    )

    assert result["kind"] == "repair"
    assert daily.is_dir()
    assert events[0].startswith("create:repair-2026-09-04-")
    assert events[1].startswith("verify:repair-2026-09-04-")
    assert events[-3:] == ["prune-daily", "prune-transient", "receipt"]


def test_second_same_day_closeout_creates_second_repair_and_preserves_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, ownership = ownership_fixture(tmp_path)
    snapshots = Path(ownership["snapshotRoot"]) / "snapshots"
    daily = snapshots / "daily-2026-09-04"
    repair_one = snapshots / "repair-2026-09-04-070000-post-index"
    daily.mkdir(parents=True)
    repair_one.mkdir()
    created: list[str] = []

    monkeypatch.setattr(runner, "trusted_closeout", lambda *_: ("2026-09-04T02:00:00+00:00", 9))

    def verification(path: Path):
        if path == daily:
            return {"createdAt": "2026-09-04T00:00:00+00:00"}
        if path == repair_one:
            return {"createdAt": "2026-09-04T01:00:00+00:00"}
        return {"createdAt": "2026-09-04T03:00:00+00:00"}

    monkeypatch.setattr(runner, "verify_snapshot", verification)

    def create(_project: Path, root: Path, name: str, _required: list[str]):
        created.append(name)
        (root / "snapshots" / name).mkdir()

    monkeypatch.setattr(runner, "create_snapshot", create)
    monkeypatch.setattr(runner, "verify_complete", lambda *_: None)
    monkeypatch.setattr(runner, "prune_daily_snapshots", lambda *_: None)
    monkeypatch.setattr(runner, "prune_transient_snapshots", lambda *_: None)
    monkeypatch.setattr(runner, "write_receipt", lambda *_: None)

    result = runner.run_snapshot(
        manifest, now=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    )

    assert result["kind"] == "repair"
    assert repair_one.is_dir()
    assert created == ["repair-2026-09-04-080000-post-index"]


def test_tampered_existing_repair_fails_closed_instead_of_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, ownership = ownership_fixture(tmp_path)
    snapshots = Path(ownership["snapshotRoot"]) / "snapshots"
    daily = snapshots / "daily-2026-09-04"
    repair = snapshots / "repair-2026-09-04-070000-post-index"
    daily.mkdir(parents=True)
    repair.mkdir()
    mutations: list[str] = []
    monkeypatch.setattr(runner, "trusted_closeout", lambda *_: ("2026-09-04T02:00:00+00:00", 9))

    def verification(path: Path):
        if path == daily:
            return {"createdAt": "2026-09-04T00:00:00+00:00"}
        raise SystemExit("tampered repair")

    monkeypatch.setattr(runner, "verify_snapshot", verification)
    monkeypatch.setattr(runner, "create_snapshot", lambda *_: mutations.append("create"))
    monkeypatch.setattr(runner, "prune_daily_snapshots", lambda *_: mutations.append("prune"))
    monkeypatch.setattr(runner, "write_receipt", lambda *_: mutations.append("receipt"))

    with pytest.raises(SystemExit, match="tampered repair"):
        runner.run_snapshot(manifest, now=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc))
    assert mutations == []


def test_invalid_existing_daily_blocks_without_retention_or_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, ownership = ownership_fixture(tmp_path)
    daily = Path(ownership["snapshotRoot"]) / "snapshots/daily-2026-09-04"
    daily.mkdir(parents=True)
    marker = daily / "keep.txt"
    marker.write_text("immutable", encoding="utf-8")
    mutations: list[str] = []

    monkeypatch.setattr(runner, "trusted_closeout", lambda *_: ("2026-09-04T01:00:00+00:00", 7))
    monkeypatch.setattr(runner, "verify_snapshot", lambda _: (_ for _ in ()).throw(SystemExit("tampered")))
    monkeypatch.setattr(runner, "create_snapshot", lambda *_: mutations.append("create"))
    monkeypatch.setattr(runner, "prune_daily_snapshots", lambda *_: mutations.append("prune"))
    monkeypatch.setattr(runner, "write_receipt", lambda *_: mutations.append("receipt"))

    with pytest.raises(SystemExit, match="tampered"):
        runner.run_snapshot(manifest, now=datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc))

    assert marker.read_text(encoding="utf-8") == "immutable"
    assert mutations == []


@pytest.mark.parametrize("broken", [False, True])
def test_contract_rejects_snapshot_root_symlink_and_broken_symlink(tmp_path: Path, broken: bool) -> None:
    manifest, ownership = ownership_fixture(tmp_path)
    root = Path(ownership["snapshotRoot"])
    root.rmdir()
    target = tmp_path / ("missing-target" if broken else "outside")
    if not broken:
        target.mkdir(mode=0o700)
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symbolic"):
        runner.validate_contract(manifest)


def test_contract_check_accepts_activation_pending_but_snapshot_run_does_not(tmp_path: Path) -> None:
    manifest, _ = ownership_fixture(tmp_path, phase="activation_pending")

    assert runner.validate_contract(manifest)["provider"] == "qwen-local"
    with pytest.raises(RuntimeError, match="not committed"):
        runner.validate_contract(manifest, create_snapshot_root=True)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:18888",
        "http://127.0.0.1:18888@external.invalid",
        "http://127.0.0.1:18888/path",
        "http://127.0.0.1:18888?redirect=external",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_snapshot_contract_rejects_non_exact_loopback_endpoint(tmp_path: Path, endpoint: str) -> None:
    manifest, ownership = ownership_fixture(tmp_path)
    source_map = Path(ownership["projectRoot"]) / "config/source-map.json"
    payload = json.loads(source_map.read_text(encoding="utf-8"))
    payload["embedding"]["endpoint"] = endpoint
    source_map.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="endpoint"):
        runner.validate_contract(manifest)


def test_snapshot_contract_requires_exact_health_and_declaration_identities(tmp_path: Path) -> None:
    manifest, _ = ownership_fixture(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["ownership"]["healthReceiptSchema"] = "backup-health-component.v0"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="contract identity"):
        runner.validate_contract(manifest)
