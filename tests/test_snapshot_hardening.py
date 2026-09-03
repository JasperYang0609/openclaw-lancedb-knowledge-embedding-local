import importlib.util
import json
import shutil
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
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert snapshot.verify_snapshot(created)["databasePath"] == "data/lancedb"


def test_manifest_database_path_is_allowlisted(tmp_path: Path):
    project = fixture_project(tmp_path)
    backup = tmp_path / "backup"
    snapshot.create_snapshot(project, backup, "unsafe-manifest")
    created = backup / "snapshots/unsafe-manifest"
    manifest_path = created / snapshot.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["databasePath"] = "../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

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
