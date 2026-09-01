import importlib.util
import json
from datetime import date
from pathlib import Path


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
