#!/usr/bin/env python3
"""Create or verify a checksummed, restore-oriented knowledge index snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


REQUIRED_PATHS = (
    "data/lancedb",
    "config/source-map.json",
    "src/metadata.js",
    "package.json",
)

OPTIONAL_PATHS = (
    "data/index-state.json",
    "data/embedding-cache",
    "data/enrichment/validated.jsonl",
    "config/source-map.example.json",
    "config/enrichment-contract.md",
    "src/security.js",
    "package-lock.json",
    "reports/index-manifest.latest.json",
    "reports/incremental-manifest.latest.json",
    "reports/source-scan.latest.json",
    "reports/benchmark.latest.json",
)

MANIFEST_NAME = "snapshot-manifest.json"
CHECKSUM_NAME = "CHECKSUMS.sha256"
DAILY_SNAPSHOT_RE = re.compile(r"^daily-(\d{4}-\d{2}-\d{2})$")
TRANSIENT_SNAPSHOT_RE = re.compile(r"^(incident|repair)-(\d{4}-\d{2}-\d{2})(?:-|$)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def payload_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name not in {MANIFEST_NAME, CHECKSUM_NAME}
    )


def reject_symlinks(path: Path) -> None:
    if path.is_symlink() or any(child.is_symlink() for child in path.rglob("*")):
        raise SystemExit(f"Symlinks are not allowed in knowledge snapshots: {path}")


def copy_asset(project: Path, staging: Path, relative: str) -> None:
    source = project / relative
    target = staging / relative
    reject_symlinks(source)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def write_manifest(
    snapshot: Path,
    project: Path,
    missing_optional: list[str],
    required_after: list[str] | None = None,
) -> dict:
    rows = []
    total_bytes = 0
    for file_path in payload_files(snapshot):
        relative = file_path.relative_to(snapshot).as_posix()
        size = file_path.stat().st_size
        total_bytes += size
        rows.append({"path": relative, "bytes": size, "sha256": sha256(file_path)})

    manifest = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "projectName": project.name,
        "files": len(rows),
        "bytes": total_bytes,
        "missingOptional": missing_optional,
        "requiredAfter": required_after or [],
        "assets": rows,
    }
    (snapshot / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (snapshot / CHECKSUM_NAME).write_text(
        "".join(f"{row['sha256']}  {row['path']}\n" for row in rows), encoding="utf-8"
    )
    return manifest


def verify_snapshot(snapshot: Path) -> dict:
    manifest_path = snapshot / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit(f"Snapshot manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    seen = set()
    total_bytes = 0
    for row in manifest.get("assets", []):
        relative = row.get("path", "")
        if not relative or relative in seen or relative.startswith("/") or ".." in Path(relative).parts:
            errors.append(f"invalid manifest path: {relative!r}")
            continue
        seen.add(relative)
        file_path = snapshot / relative
        if not file_path.is_file():
            errors.append(f"missing: {relative}")
            continue
        size = file_path.stat().st_size
        total_bytes += size
        if size != int(row.get("bytes", -1)):
            errors.append(f"size mismatch: {relative}")
        if sha256(file_path) != row.get("sha256"):
            errors.append(f"checksum mismatch: {relative}")

    actual = {path.relative_to(snapshot).as_posix() for path in payload_files(snapshot)}
    for extra in sorted(actual - seen):
        errors.append(f"untracked payload: {extra}")
    if len(seen) != int(manifest.get("files", -1)):
        errors.append("file count mismatch")
    if total_bytes != int(manifest.get("bytes", -1)):
        errors.append("byte count mismatch")

    result = {
        "ok": not errors,
        "snapshot": str(snapshot.resolve()),
        "files": len(seen),
        "bytes": total_bytes,
        "errors": errors,
        "createdAt": manifest.get("createdAt"),
        "manifestSha256": sha256(manifest_path),
    }
    if errors:
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SystemExit(f"Timestamp must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def freshness_gate(created_at: str | None, required_after: list[str]) -> dict:
    if not created_at:
        raise SystemExit("Snapshot manifest has no createdAt")
    created = parse_timestamp(created_at)
    required = [parse_timestamp(value) for value in required_after]
    latest = max(required) if required else None
    passed = latest is None or created > latest
    result = {
        "snapshotCreatedAt": created.isoformat(),
        "requiredAfter": latest.isoformat() if latest else None,
        "pass": passed,
    }
    if not passed:
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def resolve_snapshot_path(raw: str, expected_snapshot_root: str | None) -> Path:
    lexical = Path(raw).expanduser()
    if not lexical.is_absolute():
        raise SystemExit("--verify-snapshot must be an absolute path")
    snapshot = lexical.resolve()
    if expected_snapshot_root:
        root = Path(expected_snapshot_root).expanduser()
        if not root.is_absolute():
            raise SystemExit("--expected-snapshot-root must be an absolute path")
        snapshots_root = root.resolve() / "snapshots"
        if snapshot.parent != snapshots_root:
            raise SystemExit(f"Snapshot is outside expected root: {snapshot}")
    return snapshot


def restore_canary(snapshot: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="knowledge-snapshot-restore-") as tmp:
        restored = Path(tmp) / snapshot.name
        shutil.copytree(snapshot, restored)
        result = verify_snapshot(restored)
        result["sourceSnapshot"] = str(snapshot)
        result["restoreCanary"] = True
        return result


def verify_database(project: Path, snapshot: Path, table_name: str, expected_rows: int | None) -> dict:
    node = shutil.which("node")
    if not node:
        raise SystemExit("node executable is required for --verify-db")
    script = (
        "import * as lancedb from '@lancedb/lancedb';"
        "const db=await lancedb.connect(process.argv[1]);"
        "const table=await db.openTable(process.argv[2]);"
        "console.log(await table.countRows());"
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script, str(snapshot / "data/lancedb"), table_name],
        cwd=project,
        text=True,
        capture_output=True,
        shell=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"Snapshot database open failed: {(proc.stderr or proc.stdout).strip()}")
    rows = int(proc.stdout.strip())
    passed = expected_rows is None or rows == expected_rows
    result = {"databaseOpenPass": True, "tableName": table_name, "rows": rows, "expectedRows": expected_rows, "rowCountPass": passed}
    if not passed:
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def create_snapshot(
    project: Path, backup_root: Path, snapshot_name: str, required_after: list[str] | None = None
) -> dict:
    missing_required = [relative for relative in REQUIRED_PATHS if not (project / relative).exists()]
    if missing_required:
        raise SystemExit(f"Required knowledge assets are missing: {', '.join(missing_required)}")
    target = backup_root / "snapshots" / snapshot_name
    if target.exists():
        raise SystemExit(f"Snapshot already exists: {target}; choose a different --snapshot-name")
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{snapshot_name}-", dir=target.parent) as tmp_dir:
        staging = Path(tmp_dir) / snapshot_name
        staging.mkdir()
        for relative in REQUIRED_PATHS:
            copy_asset(project, staging, relative)
        missing_optional = []
        for relative in OPTIONAL_PATHS:
            if (project / relative).exists():
                copy_asset(project, staging, relative)
            else:
                missing_optional.append(relative)
        manifest = write_manifest(staging, project, missing_optional, required_after)
        verify_snapshot(staging)
        os.replace(staging, target)

    verification = verify_snapshot(target)
    freshness = freshness_gate(verification["createdAt"], required_after or [])
    return {"ok": True, "created": True, **verification, "freshness": freshness, "missingOptional": manifest["missingOptional"]}


def prune_daily_snapshots(backup_root: Path, retention_days: int, reference_day: date) -> dict:
    if retention_days < 1:
        raise SystemExit("--retention-days must be at least 1")

    snapshots_root = (backup_root / "snapshots").resolve()
    cutoff = reference_day - timedelta(days=retention_days - 1)
    removed: list[str] = []
    retained: list[str] = []
    ignored: list[str] = []

    if not snapshots_root.exists():
        return {
            "retentionDays": retention_days,
            "referenceDate": reference_day.isoformat(),
            "cutoffDate": cutoff.isoformat(),
            "removed": removed,
            "retained": retained,
            "ignored": ignored,
        }

    if snapshots_root.is_symlink() or not snapshots_root.is_dir():
        raise SystemExit(f"Snapshot root must be a real directory: {snapshots_root}")

    for candidate in sorted(snapshots_root.iterdir()):
        match = DAILY_SNAPSHOT_RE.fullmatch(candidate.name)
        if not match:
            ignored.append(candidate.name)
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise SystemExit(f"Daily snapshot must be a real directory: {candidate}")
        snapshot_day = date.fromisoformat(match.group(1))
        if snapshot_day < cutoff:
            resolved = candidate.resolve()
            if resolved.parent != snapshots_root:
                raise SystemExit(f"Refusing to prune outside snapshot root: {resolved}")
            shutil.rmtree(resolved)
            removed.append(candidate.name)
        else:
            retained.append(candidate.name)

    return {
        "retentionDays": retention_days,
        "referenceDate": reference_day.isoformat(),
        "cutoffDate": cutoff.isoformat(),
        "removed": removed,
        "retained": retained,
        "ignored": ignored,
    }


def prune_transient_snapshots(
    backup_root: Path,
    retention_days: int,
    max_count: int,
    reference_day: date,
) -> dict:
    if retention_days < 1 or max_count < 1:
        raise SystemExit("transient retention days/count must be at least 1")
    snapshots_root = (backup_root / "snapshots").resolve()
    cutoff = reference_day - timedelta(days=retention_days - 1)
    candidates: list[tuple[date, Path]] = []
    removed: list[str] = []
    retained: list[str] = []
    protected: list[str] = []
    if not snapshots_root.exists():
        return {"retentionDays": retention_days, "maxCombinedCount": max_count, "removed": [], "retained": [], "protected": []}
    for candidate in sorted(snapshots_root.iterdir()):
        match = TRANSIENT_SNAPSHOT_RE.match(candidate.name)
        if not match:
            continue
        if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve().parent != snapshots_root:
            raise SystemExit(f"Transient snapshot must be a real child directory: {candidate}")
        if (candidate / ".keep").exists():
            protected.append(candidate.name)
            continue
        candidates.append((date.fromisoformat(match.group(2)), candidate))
    keep = {(day, path) for day, path in sorted(candidates, reverse=True)[:max_count] if day >= cutoff}
    for row in sorted(candidates):
        day, candidate = row
        if row in keep:
            retained.append(candidate.name)
        else:
            shutil.rmtree(candidate)
            removed.append(candidate.name)
    return {
        "retentionDays": retention_days,
        "maxCombinedCount": max_count,
        "referenceDate": reference_day.isoformat(),
        "cutoffDate": cutoff.isoformat(),
        "removed": removed,
        "retained": retained,
        "protected": protected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Snapshot LanceDB, index state, embedding cache, metadata/tag rules, and restore config with SHA-256 verification."
    )
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--backup-root", help="Backup root that will contain snapshots/<name>")
    parser.add_argument("--snapshot-name", default=date.today().isoformat())
    parser.add_argument("--verify-snapshot", help="Verify an existing snapshot instead of creating one")
    parser.add_argument("--expected-snapshot-root", help="Absolute backup root used to reject misplaced relative-path verification")
    parser.add_argument("--require-after", action="append", default=[], help="Timezone-aware closeout timestamp; snapshot must be newer than all supplied values")
    parser.add_argument("--restore-canary", action="store_true", help="Copy to a temporary directory and verify the restored snapshot")
    parser.add_argument("--verify-db", action="store_true", help="Open the copied LanceDB table from the snapshot")
    parser.add_argument("--table-name", default="knowledge_chunks")
    parser.add_argument("--expected-row-count", type=int)
    parser.add_argument(
        "--retention-days",
        type=int,
        help="After a successful create or verify, remove only daily-YYYY-MM-DD snapshots outside this rolling window",
    )
    parser.add_argument(
        "--retention-reference-date",
        help="Reference date for retention in YYYY-MM-DD format; defaults to the local calendar date",
    )
    parser.add_argument("--transient-retention-days", type=int, help="Retention for incident-* and repair-* snapshots")
    parser.add_argument("--transient-max-count", type=int, help="Maximum combined incident-* and repair-* snapshots")
    args = parser.parse_args()

    reference_day = date.fromisoformat(args.retention_reference_date) if args.retention_reference_date else date.today()

    if args.verify_snapshot:
        snapshot = resolve_snapshot_path(args.verify_snapshot, args.expected_snapshot_root)
        result = verify_snapshot(snapshot)
        result["freshness"] = freshness_gate(result["createdAt"], args.require_after)
        if args.restore_canary:
            result["restoreCanary"] = restore_canary(snapshot)
        if args.verify_db:
            result["database"] = verify_database(
                Path(args.project_dir).expanduser().resolve(), snapshot, args.table_name, args.expected_row_count
            )
        if args.retention_days is not None:
            result["retention"] = prune_daily_snapshots(snapshot.parent.parent, args.retention_days, reference_day)
        if args.transient_retention_days is not None or args.transient_max_count is not None:
            if args.transient_retention_days is None or args.transient_max_count is None:
                parser.error("--transient-retention-days and --transient-max-count must be used together")
            result["transientRetention"] = prune_transient_snapshots(
                snapshot.parent.parent, args.transient_retention_days, args.transient_max_count, reference_day
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.backup_root:
        parser.error("--backup-root is required when creating a snapshot")
    project = Path(args.project_dir).expanduser().resolve()
    backup_root = Path(args.backup_root).expanduser().resolve()
    result = create_snapshot(project, backup_root, args.snapshot_name, args.require_after)
    snapshot = backup_root / "snapshots" / args.snapshot_name
    if args.restore_canary:
        result["restoreCanary"] = restore_canary(snapshot)
    if args.verify_db:
        result["database"] = verify_database(project, snapshot, args.table_name, args.expected_row_count)
    if args.retention_days is not None:
        result["retention"] = prune_daily_snapshots(
            Path(args.backup_root).expanduser().resolve(),
            args.retention_days,
            reference_day,
        )
    if args.transient_retention_days is not None or args.transient_max_count is not None:
        if args.transient_retention_days is None or args.transient_max_count is None:
            parser.error("--transient-retention-days and --transient-max-count must be used together")
        result["transientRetention"] = prune_transient_snapshots(
            backup_root, args.transient_retention_days, args.transient_max_count, reference_day
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
