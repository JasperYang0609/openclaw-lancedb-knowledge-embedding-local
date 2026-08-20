#!/usr/bin/env python3
"""Create or verify a checksummed, restore-oriented knowledge index snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import date, datetime, timezone
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


def write_manifest(snapshot: Path, project: Path, missing_optional: list[str]) -> dict:
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
    }
    if errors:
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def create_snapshot(project: Path, backup_root: Path, snapshot_name: str) -> dict:
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
        manifest = write_manifest(staging, project, missing_optional)
        verify_snapshot(staging)
        os.replace(staging, target)

    verification = verify_snapshot(target)
    return {"ok": True, "created": True, **verification, "missingOptional": manifest["missingOptional"]}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Snapshot LanceDB, index state, embedding cache, metadata/tag rules, and restore config with SHA-256 verification."
    )
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--backup-root", help="Backup root that will contain snapshots/<name>")
    parser.add_argument("--snapshot-name", default=date.today().isoformat())
    parser.add_argument("--verify-snapshot", help="Verify an existing snapshot instead of creating one")
    args = parser.parse_args()

    if args.verify_snapshot:
        print(json.dumps(verify_snapshot(Path(args.verify_snapshot).expanduser().resolve()), ensure_ascii=False, indent=2))
        return 0
    if not args.backup_root:
        parser.error("--backup-root is required when creating a snapshot")
    result = create_snapshot(
        Path(args.project_dir).expanduser().resolve(),
        Path(args.backup_root).expanduser().resolve(),
        args.snapshot_name,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
