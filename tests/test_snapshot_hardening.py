import importlib.util
import json
import os
import shutil
import stat
from datetime import date
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "openclaw-lancedb-knowledge-local/assets/knowledge-lancedb-template/scripts/snapshot_knowledge_assets.py"
spec = importlib.util.spec_from_file_location("snapshot_knowledge_assets_hardening", SCRIPT)
snapshot = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(snapshot)


def fixture_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for relative, content in {
        "data/lancedb/table.lance": "table",
        "config/source-map.json": '{"sources":[]}',
        "src/metadata.js": "export {};",
        "package.json": '{"name":"fixture"}',
    }.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return project


def fixture_qwen_project(tmp_path: Path) -> Path:
    project = tmp_path / "qwen-project"
    for relative, content in {
        "data/qwen-local-lancedb/table.lance": "qwen-table",
        "data/index-state.json": '{"status":"ready"}',
        "data/openclaw-ready.json": '{"ready":true,"provider":"qwen-local"}',
        "config/source-map.json": '{"sources":[]}',
        "src/metadata.js": "export {};",
        "package.json": '{"name":"qwen-fixture"}',
    }.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return project


def test_relative_snapshot_verification_path_is_rejected():
    try:
        snapshot.resolve_snapshot_path("daily-2026-08-23", None)
        raised = False
    except SystemExit as exc:
        raised = "absolute path" in str(exc)
    assert raised


def test_expected_root_rejects_snapshot_from_another_directory(tmp_path: Path):
    wrong = tmp_path / "wrong/snapshots/daily-2026-08-23"
    wrong.mkdir(parents=True)
    try:
        snapshot.resolve_snapshot_path(str(wrong), str(tmp_path / "expected"))
        raised = False
    except SystemExit as exc:
        raised = "outside expected root" in str(exc)
    assert raised


def test_snapshot_freshness_and_restore_canary(tmp_path: Path):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    result = snapshot.create_snapshot(
        project,
        backup,
        "incident-2026-08-23-post-closeout",
        ["2026-08-22T00:00:00+00:00", "2026-08-22T01:00:00+00:00"],
    )
    created = backup / "snapshots/incident-2026-08-23-post-closeout"
    assert result["freshness"]["pass"] is True
    assert result["manifestSha256"]
    assert snapshot.restore_canary(created)["ok"] is True


def test_qwen_snapshot_records_database_path_and_required_state(tmp_path: Path):
    project = fixture_qwen_project(tmp_path)
    backup = tmp_path / "backup"

    result = snapshot.create_snapshot(project, backup, "daily-2026-09-03")

    created = backup / "snapshots/daily-2026-09-03"
    manifest = json.loads((created / snapshot.MANIFEST_NAME).read_text(encoding="utf-8"))
    paths = {row["path"] for row in manifest["assets"]}
    assert result["databasePath"] == "data/qwen-local-lancedb"
    assert manifest["databasePath"] == "data/qwen-local-lancedb"
    assert "data/qwen-local-lancedb/table.lance" in paths
    assert "data/index-state.json" in paths
    assert "data/openclaw-ready.json" in paths
    assert snapshot.verify_snapshot(created)["databasePath"] == "data/qwen-local-lancedb"


def test_database_verification_uses_manifest_bound_qwen_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = fixture_qwen_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "qwen-db-verify")
    created = backup / "snapshots/qwen-db-verify"
    calls: list[list[str]] = []

    monkeypatch.setattr(snapshot.shutil, "which", lambda name: "/safe/node" if name == "node" else None)

    def fake_run(argv: list[str], **kwargs: object):
        calls.append(argv)
        return snapshot.subprocess.CompletedProcess(argv, 0, stdout="42\n", stderr="")

    monkeypatch.setattr(snapshot.subprocess, "run", fake_run)

    result = snapshot.verify_database(project, created, "knowledge_chunks_qwen_local", 42)

    assert result["databasePath"] == "data/qwen-local-lancedb"
    assert calls[0][-2] == str(created / "data/qwen-local-lancedb")


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, "41\n", "", "rowCountPass"),
        (1, "", "cannot open", "database open failed"),
    ],
)
def test_database_verification_fails_closed_on_row_mismatch_and_open_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
    expected: str,
):
    project = fixture_qwen_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "qwen-db-negative")
    created = backup / "snapshots/qwen-db-negative"
    monkeypatch.setattr(snapshot.shutil, "which", lambda name: "/safe/node" if name == "node" else None)
    monkeypatch.setattr(
        snapshot.subprocess,
        "run",
        lambda argv, **kwargs: snapshot.subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=stderr
        ),
    )

    with pytest.raises(SystemExit, match=expected):
        snapshot.verify_database(project, created, "knowledge_chunks_qwen_local", 42)


def test_database_verification_timeout_is_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = fixture_qwen_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "qwen-db-timeout")
    created = backup / "snapshots/qwen-db-timeout"
    monkeypatch.setattr(snapshot.shutil, "which", lambda name: "/safe/node" if name == "node" else None)

    def timeout(argv: list[str], **kwargs: object):
        assert kwargs["timeout"] == snapshot.DATABASE_VERIFY_TIMEOUT_SECONDS
        raise snapshot.subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(snapshot.subprocess, "run", timeout)
    with pytest.raises(SystemExit, match="bounded timeout") as error:
        snapshot.verify_database(project, created, "knowledge_chunks_qwen_local", 42)
    assert str(created) not in str(error.value)


@pytest.mark.parametrize("missing", ["data/index-state.json", "data/openclaw-ready.json"])
def test_qwen_snapshot_requires_readiness_state(tmp_path: Path, missing: str):
    project = fixture_qwen_project(tmp_path)
    (project / missing).unlink()

    with pytest.raises(SystemExit, match="Required knowledge assets are missing"):
        snapshot.create_snapshot(project, tmp_path / "backup", "qwen-missing-state")


def test_snapshot_rejects_ambiguous_database_layout(tmp_path: Path):
    project = fixture_qwen_project(tmp_path)
    legacy = project / "data/lancedb/table.lance"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")

    with pytest.raises(SystemExit, match="Exactly one supported"):
        snapshot.create_snapshot(project, tmp_path / "backup", "ambiguous")


def test_snapshot_rejects_symlinked_database_directory(tmp_path: Path):
    project = fixture_project(tmp_path)
    outside = tmp_path / "outside-db"
    outside.mkdir()
    (outside / "table.lance").write_text("outside", encoding="utf-8")
    shutil.rmtree(project / "data/lancedb")
    (project / "data/lancedb").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SystemExit, match="symbolic links"):
        snapshot.create_snapshot(project, tmp_path / "backup", "symlinked")


def test_legacy_manifest_defaults_to_legacy_database_path(tmp_path: Path):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "legacy")
    created = backup / "snapshots/legacy"
    manifest_path = created / snapshot.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("databasePath")
    manifest["schemaVersion"] = 1
    created.chmod(0o700)
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o400)
    created.chmod(0o500)

    assert snapshot.verify_snapshot(created)["databasePath"] == "data/lancedb"


def test_manifest_database_path_is_allowlisted(tmp_path: Path):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "unsafe-manifest")
    created = backup / "snapshots/unsafe-manifest"
    manifest_path = created / snapshot.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["databasePath"] = "../outside"
    created.chmod(0o700)
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o400)
    created.chmod(0o500)

    with pytest.raises(SystemExit, match="Unsupported snapshot databasePath"):
        snapshot.verify_snapshot(created)


def test_combined_transient_retention_enforces_age_and_count(tmp_path: Path):
    root = tmp_path / "backup/snapshots"
    names = [
        "incident-2026-08-01-old",
        "repair-2026-08-20-a",
        "incident-2026-08-21-b",
        "repair-2026-08-22-c",
        "incident-2026-08-23-d",
    ]
    for name in names:
        (root / name).mkdir(parents=True)
    (root / "manual-keep").mkdir()
    result = snapshot.prune_transient_snapshots(tmp_path / "backup", 7, 3, date(2026, 8, 23))
    assert result["removed"] == ["incident-2026-08-01-old", "repair-2026-08-20-a"]
    assert set(result["retained"]) == {
        "incident-2026-08-21-b", "repair-2026-08-22-c", "incident-2026-08-23-d"
    }
    assert (root / "manual-keep").is_dir()


def test_freshness_gate_rejects_snapshot_before_closeout():
    try:
        snapshot.freshness_gate("2026-08-22T00:00:00+00:00", ["2026-08-22T00:00:01+00:00"])
        raised = False
    except SystemExit as exc:
        raised = json.loads(str(exc))["pass"] is False
    assert raised


@pytest.mark.parametrize("broken", [False, True])
def test_verify_rejects_payload_symlink_and_broken_symlink(tmp_path: Path, broken: bool):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "symlink-payload")
    created = backup / "snapshots/symlink-payload"
    asset = created / "src/metadata.js"
    target = tmp_path / ("missing.js" if broken else "outside.js")
    if not broken:
        target.write_text("outside", encoding="utf-8")
    created.chmod(0o700)
    asset.parent.chmod(0o700)
    asset.unlink()
    asset.symlink_to(target)
    created.chmod(0o500)
    asset.parent.chmod(0o500)

    with pytest.raises(SystemExit, match="Symlinks"):
        snapshot.verify_snapshot(created)


@pytest.mark.parametrize("name", [snapshot.MANIFEST_NAME, snapshot.CHECKSUM_NAME])
def test_verify_rejects_symlinked_control_files(tmp_path: Path, name: str):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "symlink-control")
    created = backup / "snapshots/symlink-control"
    control = created / name
    outside = tmp_path / f"outside-{name}"
    outside.write_bytes(control.read_bytes())
    created.chmod(0o700)
    control.unlink()
    control.symlink_to(outside)
    created.chmod(0o500)

    with pytest.raises(SystemExit, match="Symlinks"):
        snapshot.verify_snapshot(created)


def test_verify_rejects_special_and_extra_entries(tmp_path: Path):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "special-entry")
    created = backup / "snapshots/special-entry"
    created.chmod(0o700)
    fifo = created / "unexpected.fifo"
    os.mkfifo(fifo)
    created.chmod(0o500)

    with pytest.raises(SystemExit, match="Special"):
        snapshot.verify_snapshot(created)


def test_checksum_file_must_exactly_match_manifest(tmp_path: Path):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "checksum-control")
    created = backup / "snapshots/checksum-control"
    checksums = created / snapshot.CHECKSUM_NAME
    created.chmod(0o700)
    checksums.chmod(0o600)
    checksums.write_text("0" * 64 + "  fake\n", encoding="utf-8")
    checksums.chmod(0o400)
    created.chmod(0o500)

    with pytest.raises(SystemExit, match="checksum manifest mismatch"):
        snapshot.verify_snapshot(created)


def test_create_blocks_existing_broken_symlink_and_finalizes_read_only(tmp_path: Path):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "immutable")
    created = backup / "snapshots/immutable"
    directories, files = snapshot._tree_entries(created)
    assert all(not (path.lstat().st_mode & 0o222) for path in [*directories, *files])
    assert snapshot.verify_snapshot(created)["ok"] is True

    broken = backup / "snapshots/broken"
    broken.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    with pytest.raises(SystemExit, match="already exists"):
        snapshot.create_snapshot(project, backup, "broken")


def test_resolve_snapshot_path_rejects_symlinked_parent(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(SystemExit, match="symbolic"):
        snapshot.resolve_snapshot_path(str(linked / "daily-2026-09-04"), None)


@pytest.mark.parametrize("broken", [False, True])
@pytest.mark.parametrize("kind", ["daily", "transient"])
def test_retention_rejects_symlinked_and_broken_collection_root(
    tmp_path: Path, broken: bool, kind: str
):
    backup = tmp_path / f"backup-{kind}-{broken}"
    backup.mkdir()
    outside = tmp_path / f"outside-{kind}-{broken}"
    if not broken:
        outside.mkdir()
    (backup / "snapshots").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SystemExit, match="symbolic"):
        if kind == "daily":
            snapshot.prune_daily_snapshots(backup, 30, date(2026, 9, 4))
        else:
            snapshot.prune_transient_snapshots(backup, 7, 10, date(2026, 9, 4))


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo"])
def test_transient_retention_rejects_unsafe_keep_marker(tmp_path: Path, entry_kind: str):
    candidate = tmp_path / "backup/snapshots/repair-2026-09-04-test"
    candidate.mkdir(parents=True)
    marker = candidate / ".keep"
    if entry_kind == "symlink":
        marker.symlink_to(tmp_path / "missing-keep")
        expected = "Symlinks"
    else:
        os.mkfifo(marker)
        expected = "Special"

    with pytest.raises(SystemExit, match=expected):
        snapshot.prune_transient_snapshots(tmp_path / "backup", 7, 10, date(2026, 9, 4))


def test_retention_can_deliberately_remove_finalized_read_only_snapshots(tmp_path: Path):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "daily-2026-08-01")
    snapshot.create_snapshot(project, backup, "repair-2026-08-01-old")

    daily = snapshot.prune_daily_snapshots(backup, 30, date(2026, 9, 4))
    transient = snapshot.prune_transient_snapshots(backup, 7, 10, date(2026, 9, 4))

    assert daily["removed"] == ["daily-2026-08-01"]
    assert transient["removed"] == ["repair-2026-08-01-old"]
    assert not (backup / "snapshots/daily-2026-08-01").exists()
    assert not (backup / "snapshots/repair-2026-08-01-old").exists()
