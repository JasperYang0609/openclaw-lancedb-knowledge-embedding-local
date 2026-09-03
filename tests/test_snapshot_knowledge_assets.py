#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "openclaw-lancedb-knowledge-local" / "assets" / "knowledge-lancedb-template" / "scripts" / "snapshot_knowledge_assets.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="knowledge-snapshot-test-") as tmp_dir:
        tmp = Path(tmp_dir)
        project = tmp / "project"
        backup = tmp / "backup"
        for relative, content in {
            "data/lancedb/table.lance": "table-bytes",
            "data/index-state.json": "{}\n",
            "data/embedding-cache/cache.jsonl": '{"vector":[1]}\n',
            "config/source-map.json": '{"sources":[]}\n',
            "src/metadata.js": "export const metadata = true;\n",
            "package.json": '{"name":"fixture"}\n',
        }.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        result = run(
            "--project-dir", str(project),
            "--backup-root", str(backup),
            "--snapshot-name", "fixture",
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        snapshot = backup / "snapshots" / "fixture"
        manifest = json.loads((snapshot / "snapshot-manifest.json").read_text(encoding="utf-8"))
        paths = {row["path"] for row in manifest["assets"]}
        assert manifest["databasePath"] == "data/lancedb"
        assert "data/lancedb/table.lance" in paths
        assert "data/embedding-cache/cache.jsonl" in paths
        assert "src/metadata.js" in paths
        assert (snapshot / "CHECKSUMS.sha256").is_file()

        verify = run("--verify-snapshot", str(snapshot))
        assert verify.returncode == 0, verify.stderr
        assert json.loads(verify.stdout)["ok"] is True

        snapshots_root = backup / "snapshots"
        for name in (
            "daily-2026-01-01",
            "daily-2026-01-02",
            "daily-2026-01-30",
            "daily-2026-01-31",
            "manual-2026-01-01",
        ):
            (snapshots_root / name).mkdir()
        retained = run(
            "--verify-snapshot", str(snapshot),
            "--retention-days", "30",
            "--retention-reference-date", "2026-01-31",
        )
        assert retained.returncode == 0, retained.stderr
        retention = json.loads(retained.stdout)["retention"]
        assert retention["cutoffDate"] == "2026-01-02"
        assert retention["removed"] == ["daily-2026-01-01"]
        assert (snapshots_root / "daily-2026-01-02").is_dir()
        assert (snapshots_root / "daily-2026-01-30").is_dir()
        assert (snapshots_root / "daily-2026-01-31").is_dir()
        assert (snapshots_root / "manual-2026-01-01").is_dir()

        (snapshot / "src/metadata.js").write_text("tampered\n", encoding="utf-8")
        verify = run("--verify-snapshot", str(snapshot))
        assert verify.returncode != 0
        assert "checksum mismatch" in (verify.stderr + verify.stdout)

    print("PASS test_snapshot_knowledge_assets")


if __name__ == "__main__":
    main()
