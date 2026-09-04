#!/usr/bin/env python3
"""Create or verify a checksummed, restore-oriented knowledge index snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


SUPPORTED_DATABASE_PATHS = (
    "data/lancedb",
    "data/qwen-local-lancedb",
)

REQUIRED_PATHS = (
    "config/source-map.json",
    "src/metadata.js",
    "package.json",
)

QWEN_REQUIRED_PATHS = (
    "data/index-state.json",
    "data/openclaw-ready.json",
)

OPTIONAL_PATHS = (
    "data/index-state.json",
    "data/openclaw-ready.json",
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
SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DATABASE_VERIFY_TIMEOUT_SECONDS = 120


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SystemExit(f"Snapshot asset must be a single regular file: {path}")
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(handle.fileno())
            identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, key) != getattr(after, key) for key in identity):
                raise SystemExit(f"Snapshot asset changed while being verified: {path}")
    except OSError as error:
        raise SystemExit(f"Snapshot asset is missing or unsafe: {path}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def _tree_entries(root: Path) -> tuple[list[Path], list[Path]]:
    if root.is_symlink() or not root.is_dir():
        raise SystemExit(f"Snapshot must be a real directory: {root}")
    directories: list[Path] = [root]
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        try:
            metadata = path.lstat()
        except OSError as error:
            raise SystemExit(f"Snapshot entry is missing or unsafe: {path}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise SystemExit(f"Symlinks are not allowed in knowledge snapshots: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(path)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            files.append(path)
        else:
            raise SystemExit(f"Special or hard-linked entries are not allowed in snapshots: {path}")
    return directories, files


def payload_files(root: Path) -> list[Path]:
    _, files = _tree_entries(root)
    return [path for path in files if path.name not in {MANIFEST_NAME, CHECKSUM_NAME}]


def reject_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise SystemExit(f"Symlinks are not allowed in knowledge snapshots: {path}")
    if path.is_dir():
        _tree_entries(path)
    elif path.is_file():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SystemExit(f"Snapshot source must be a single regular file: {path}")
    else:
        raise SystemExit(f"Snapshot source is missing or unsafe: {path}")


def make_tree_immutable(root: Path) -> None:
    directories, files = _tree_entries(root)
    for path in files:
        os.chmod(path, 0o400)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        os.chmod(path, 0o500)


def remove_snapshot_tree(snapshot: Path, snapshots_root: Path) -> None:
    """Deliberately unseal and remove one verified real child selected by retention."""
    root = snapshots_root.resolve()
    if snapshots_root.is_symlink() or not snapshots_root.is_dir() \
            or snapshot.is_symlink() or not snapshot.is_dir() or snapshot.resolve().parent != root:
        raise SystemExit(f"Refusing to prune unsafe snapshot path: {snapshot}")
    directories, files = _tree_entries(snapshot)
    if any(path.lstat().st_uid != os.getuid() for path in [*directories, *files]):
        raise SystemExit(f"Refusing to prune snapshot with foreign ownership: {snapshot}")
    try:
        for path in files:
            os.chmod(path, 0o600)
        for path in directories:
            os.chmod(path, 0o700)
        shutil.rmtree(snapshot)
    except Exception:
        if snapshot.exists() and not snapshot.is_symlink():
            try:
                make_tree_immutable(snapshot)
            except Exception:
                pass
        raise


def _assert_immutable(root: Path, directories: list[Path], files: list[Path]) -> None:
    for path in [*directories, *files]:
        if path.lstat().st_mode & 0o222:
            raise SystemExit(f"Finalized snapshot entry is writable: {path.relative_to(root)}")


def _source_identity(path: Path) -> dict[str, tuple[int, int, int, int, int, int, int]]:
    reject_symlinks(path)
    entries = [path, *sorted(path.rglob("*"))] if path.is_dir() else [path]
    identity: dict[str, tuple[int, int, int, int, int, int, int]] = {}
    for entry in entries:
        metadata = entry.lstat()
        if metadata.st_uid != os.getuid():
            raise SystemExit(f"Snapshot source ownership is unsafe: {entry}")
        relative = "." if entry == path else entry.relative_to(path).as_posix()
        identity[relative] = (
            stat.S_IFMT(metadata.st_mode), metadata.st_dev, metadata.st_ino,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_nlink,
        )
    return identity


def copy_asset(project: Path, staging: Path, relative: str) -> None:
    source = project / relative
    target = staging / relative
    before = _source_identity(source)
    try:
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
            reject_symlinks(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            source_fd: int | None = None
            target_fd: int | None = None
            try:
                source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                source_meta = os.fstat(source_fd)
                if not stat.S_ISREG(source_meta.st_mode) or source_meta.st_uid != os.getuid() \
                        or source_meta.st_nlink != 1:
                    raise SystemExit(f"Snapshot source is unsafe: {source}")
                target_fd = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
                )
                while True:
                    block = os.read(source_fd, 1024 * 1024)
                    if not block:
                        break
                    view = memoryview(block)
                    while view:
                        written = os.write(target_fd, view)
                        view = view[written:]
                os.fsync(target_fd)
                source_after = os.fstat(source_fd)
                stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                if any(getattr(source_meta, field) != getattr(source_after, field) for field in stable_fields):
                    raise SystemExit(f"Snapshot source changed while being copied: {source}")
            finally:
                if source_fd is not None:
                    os.close(source_fd)
                if target_fd is not None:
                    os.close(target_fd)
        after = _source_identity(source)
        if after != before:
            raise SystemExit(f"Snapshot source changed while being copied: {source}")
    except Exception:
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        raise


def _validate_real_relative_path(root: Path, relative: str, *, directory: bool) -> Path:
    """Resolve a fixed relative path without allowing a symlink escape."""
    candidate = root
    for part in Path(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise SystemExit(f"Snapshot asset must not use symbolic links: {relative}")
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved_root not in resolved.parents:
        raise SystemExit(f"Snapshot asset escapes the project root: {relative}")
    if directory:
        if not candidate.is_dir():
            raise SystemExit(f"Snapshot database must be a real directory: {relative}")
    elif not candidate.is_file():
        raise SystemExit(f"Required snapshot asset must be a real file: {relative}")
    return candidate


def resolve_project_database_path(project: Path) -> str:
    """Return the one supported real database directory present in a project."""
    if project.is_symlink() or not project.is_dir():
        raise SystemExit(f"Project must be a real directory: {project}")
    present: list[str] = []
    for relative in SUPPORTED_DATABASE_PATHS:
        candidate = project / relative
        if candidate.exists() or candidate.is_symlink():
            _validate_real_relative_path(project, relative, directory=True)
            present.append(relative)
    if len(present) != 1:
        found = ", ".join(present) if present else "none"
        raise SystemExit(
            "Exactly one supported knowledge database directory is required; "
            f"found: {found}"
        )
    return present[0]


def manifest_database_path(manifest: dict) -> str:
    """Read the allowlisted database path, preserving legacy manifest support."""
    relative = manifest.get("databasePath", "data/lancedb")
    if not isinstance(relative, str) or relative not in SUPPORTED_DATABASE_PATHS:
        raise SystemExit(f"Unsupported snapshot databasePath: {relative!r}")
    return relative


def validate_snapshot_database_path(snapshot: Path, manifest: dict) -> str:
    declared = manifest_database_path(manifest)
    detected = resolve_project_database_path(snapshot)
    if detected != declared:
        raise SystemExit(
            f"Snapshot databasePath mismatch: manifest={declared!r}, detected={detected!r}"
        )
    return declared


def write_manifest(
    snapshot: Path,
    project: Path,
    missing_optional: list[str],
    database_path: str,
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
        "schemaVersion": 2,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "projectName": project.name,
        "databasePath": database_path,
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


def verify_snapshot(snapshot: Path, *, require_immutable: bool = True) -> dict:
    directories, all_files = _tree_entries(snapshot)
    if require_immutable:
        _assert_immutable(snapshot, directories, all_files)
    manifest_path = snapshot / MANIFEST_NAME
    checksum_path = snapshot / CHECKSUM_NAME
    if manifest_path not in all_files or checksum_path not in all_files:
        raise SystemExit(f"Snapshot manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit("Snapshot manifest must be a JSON object")
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise SystemExit("Snapshot manifest assets must be an array")
    database_path = validate_snapshot_database_path(snapshot, manifest)
    errors = []
    seen = set()
    total_bytes = 0
    checksum_lines: list[str] = []
    identities: dict[str, tuple[int, int, int, int, int]] = {}
    for row in assets:
        if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
            errors.append("invalid manifest asset schema")
            continue
        relative = row.get("path", "")
        if not isinstance(relative, str) or not relative or relative in seen \
                or Path(relative).is_absolute() or ".." in Path(relative).parts:
            errors.append(f"invalid manifest path: {relative!r}")
            continue
        seen.add(relative)
        file_path = snapshot / relative
        try:
            metadata = file_path.lstat()
        except OSError:
            errors.append(f"missing: {relative}")
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            errors.append(f"unsafe file: {relative}")
            continue
        size = file_path.stat().st_size
        total_bytes += size
        if type(row.get("bytes")) is not int or size != row["bytes"]:
            errors.append(f"size mismatch: {relative}")
        expected_hash = row.get("sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            errors.append(f"invalid checksum: {relative}")
        elif sha256(file_path) != expected_hash:
            errors.append(f"checksum mismatch: {relative}")
        else:
            checksum_lines.append(f"{expected_hash}  {relative}\n")
        identities[relative] = (
            metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns,
        )

    actual_files = payload_files(snapshot)
    actual = {path.relative_to(snapshot).as_posix() for path in actual_files}
    for extra in sorted(actual - seen):
        errors.append(f"untracked payload: {extra}")
    if len(seen) != int(manifest.get("files", -1)):
        errors.append("file count mismatch")
    if total_bytes != int(manifest.get("bytes", -1)):
        errors.append("byte count mismatch")
    try:
        checksum_text = checksum_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        checksum_text = ""
    if checksum_text != "".join(checksum_lines):
        errors.append("checksum manifest mismatch")
    for relative, identity in identities.items():
        current = (snapshot / relative).lstat()
        current_identity = (
            current.st_dev, current.st_ino, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns,
        )
        if current_identity != identity:
            errors.append(f"file changed during verification: {relative}")
    if database_path == "data/qwen-local-lancedb":
        for relative in QWEN_REQUIRED_PATHS:
            required = snapshot / relative
            if required.is_symlink() or not required.is_file() or relative not in seen:
                errors.append(f"missing required Qwen state: {relative}")

    result = {
        "ok": not errors,
        "snapshot": str(snapshot.resolve()),
        "files": len(seen),
        "bytes": total_bytes,
        "errors": errors,
        "createdAt": manifest.get("createdAt"),
        "databasePath": database_path,
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
    if lexical.is_symlink() or any(parent.is_symlink() for parent in lexical.parents):
        raise SystemExit("--verify-snapshot path must not contain symbolic links")
    if not os.path.lexists(lexical):
        raise SystemExit("--verify-snapshot path does not exist")
    snapshot = lexical.resolve()
    if expected_snapshot_root:
        root = Path(expected_snapshot_root).expanduser()
        if not root.is_absolute():
            raise SystemExit("--expected-snapshot-root must be an absolute path")
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            raise SystemExit("--expected-snapshot-root must not contain symbolic links")
        snapshots_root = root.resolve() / "snapshots"
        if snapshot.parent != snapshots_root:
            raise SystemExit(f"Snapshot is outside expected root: {snapshot}")
    return snapshot


def restore_canary(snapshot: Path) -> dict:
    verify_snapshot(snapshot)
    with tempfile.TemporaryDirectory(prefix="knowledge-snapshot-restore-") as tmp:
        restored = Path(tmp) / snapshot.name
        shutil.copytree(snapshot, restored)
        result = verify_snapshot(restored)
        result["sourceSnapshot"] = str(snapshot)
        result["restoreCanary"] = True
        return result


def verify_database(project: Path, snapshot: Path, table_name: str, expected_rows: int | None) -> dict:
    verify_snapshot(snapshot)
    node = shutil.which("node")
    if not node:
        raise SystemExit("node executable is required for --verify-db")
    script = (
        "import * as lancedb from '@lancedb/lancedb';"
        "const db=await lancedb.connect(process.argv[1]);"
        "const table=await db.openTable(process.argv[2]);"
        "console.log(await table.countRows());"
    )
    manifest_path = snapshot / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit(f"Snapshot manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit("Snapshot manifest must be a JSON object")
    database_path = validate_snapshot_database_path(snapshot, manifest)
    try:
        proc = subprocess.run(
            [node, "--input-type=module", "-e", script, str(snapshot / database_path), table_name],
            cwd=project,
            text=True,
            capture_output=True,
            shell=False,
            timeout=DATABASE_VERIFY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise SystemExit("Snapshot database verification exceeded the bounded timeout") from error
    if proc.returncode != 0:
        raise SystemExit("Snapshot database open failed")
    try:
        rows = int(proc.stdout.strip())
    except (TypeError, ValueError) as error:
        raise SystemExit("Snapshot database row count output is invalid") from error
    passed = expected_rows is None or rows == expected_rows
    result = {
        "databaseOpenPass": True,
        "databasePath": database_path,
        "tableName": table_name,
        "rows": rows,
        "expectedRows": expected_rows,
        "rowCountPass": passed,
    }
    if not passed:
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def create_snapshot(
    project: Path, backup_root: Path, snapshot_name: str, required_after: list[str] | None = None
) -> dict:
    if not SNAPSHOT_NAME_RE.fullmatch(snapshot_name) or snapshot_name in {".", ".."}:
        raise SystemExit("Snapshot name is invalid")
    if backup_root.is_symlink() or any(parent.is_symlink() for parent in backup_root.parents):
        raise SystemExit("Snapshot root must not contain symbolic links")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    snapshots_root = backup_root / "snapshots"
    if os.path.lexists(snapshots_root) and (snapshots_root.is_symlink() or not snapshots_root.is_dir()):
        raise SystemExit("Snapshot collection root is unsafe")
    snapshots_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(snapshots_root, 0o700)
    database_path = resolve_project_database_path(project)
    provider_required = QWEN_REQUIRED_PATHS if database_path == "data/qwen-local-lancedb" else ()
    required_paths = (*REQUIRED_PATHS, database_path, *provider_required)
    missing_required = [relative for relative in required_paths if not (project / relative).exists()]
    if missing_required:
        raise SystemExit(f"Required knowledge assets are missing: {', '.join(missing_required)}")
    target = snapshots_root / snapshot_name
    if os.path.lexists(target):
        raise SystemExit(f"Snapshot already exists: {target}; choose a different --snapshot-name")

    with tempfile.TemporaryDirectory(prefix=f".{snapshot_name}-", dir=target.parent) as tmp_dir:
        staging = Path(tmp_dir) / snapshot_name
        staging.mkdir()
        for relative in required_paths:
            copy_asset(project, staging, relative)
        missing_optional = []
        for relative in OPTIONAL_PATHS:
            if relative in required_paths:
                continue
            if os.path.lexists(project / relative):
                copy_asset(project, staging, relative)
            else:
                missing_optional.append(relative)
        manifest = write_manifest(
            staging, project, missing_optional, database_path, required_after
        )
        verify_snapshot(staging, require_immutable=False)
        try:
            shutil.copytree(staging, target)
            make_tree_immutable(target)
        except Exception:
            if target.exists() and not target.is_symlink():
                shutil.rmtree(target)
            raise

    verification = verify_snapshot(target)
    freshness = freshness_gate(verification["createdAt"], required_after or [])
    return {"ok": True, "created": True, **verification, "freshness": freshness, "missingOptional": manifest["missingOptional"]}


def prune_daily_snapshots(backup_root: Path, retention_days: int, reference_day: date) -> dict:
    if retention_days < 1:
        raise SystemExit("--retention-days must be at least 1")

    lexical_root = backup_root / "snapshots"
    cutoff = reference_day - timedelta(days=retention_days - 1)
    removed: list[str] = []
    retained: list[str] = []
    ignored: list[str] = []

    if not os.path.lexists(lexical_root):
        return {
            "retentionDays": retention_days,
            "referenceDate": reference_day.isoformat(),
            "cutoffDate": cutoff.isoformat(),
            "removed": removed,
            "retained": retained,
            "ignored": ignored,
        }

    if lexical_root.is_symlink() or any(parent.is_symlink() for parent in lexical_root.parents):
        raise SystemExit(f"Snapshot root must not contain symbolic links: {lexical_root}")
    metadata = lexical_root.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f"Snapshot root must be a real directory: {lexical_root}")
    _tree_entries(lexical_root)
    snapshots_root = lexical_root.resolve()

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
            remove_snapshot_tree(resolved, snapshots_root)
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
    lexical_root = backup_root / "snapshots"
    cutoff = reference_day - timedelta(days=retention_days - 1)
    candidates: list[tuple[date, Path]] = []
    removed: list[str] = []
    retained: list[str] = []
    protected: list[str] = []
    if not os.path.lexists(lexical_root):
        return {"retentionDays": retention_days, "maxCombinedCount": max_count, "removed": [], "retained": [], "protected": []}
    if lexical_root.is_symlink() or any(parent.is_symlink() for parent in lexical_root.parents):
        raise SystemExit(f"Snapshot root must not contain symbolic links: {lexical_root}")
    metadata = lexical_root.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f"Snapshot root must be a real directory: {lexical_root}")
    _tree_entries(lexical_root)
    snapshots_root = lexical_root.resolve()
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
            remove_snapshot_tree(candidate, snapshots_root)
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
