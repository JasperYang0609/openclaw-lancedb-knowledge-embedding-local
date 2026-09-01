from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


class UnsafeArchiveError(RuntimeError):
    pass


def _safe_member(member: tarfile.TarInfo, expected_top: str) -> PurePosixPath:
    name = PurePosixPath(member.name)
    if name.is_absolute() or not name.parts or ".." in name.parts or name.parts[0] != expected_top:
        raise UnsafeArchiveError(f"unsafe archive path: {member.name}")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise UnsafeArchiveError(f"unsupported archive entry: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise UnsafeArchiveError(f"unknown archive entry type: {member.name}")
    return name


def extract_verified_tar(
    archive_path: str | Path,
    destination: str | Path,
    *,
    expected_top: str = "llama-b10625",
) -> list[dict]:
    archive = Path(archive_path).resolve()
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() or target.is_symlink():
        raise UnsafeArchiveError("runtime destination already exists")
    staging = Path(tempfile.mkdtemp(prefix=".qwen-runtime-staging-", dir=target.parent))
    inventory: list[dict] = []
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            names: set[PurePosixPath] = set()
            checked: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            for member in members:
                name = _safe_member(member, expected_top)
                if name in names:
                    raise UnsafeArchiveError(f"duplicate archive path: {member.name}")
                names.add(name)
                checked.append((member, name))
            regular_names = {str(name) for member, name in checked if member.isfile()}
            if not any(name.endswith("/llama-server") for name in regular_names):
                raise UnsafeArchiveError("archive is missing llama-server")
            if not any(PurePosixPath(name).name.upper().startswith("LICENSE") for name in regular_names):
                raise UnsafeArchiveError("archive is missing LICENSE")
            for member, name in checked:
                relative = Path(*name.parts[1:])
                output = staging / relative
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = bundle.extractfile(member)
                if source is None:
                    raise UnsafeArchiveError(f"cannot read archive entry: {member.name}")
                digest = hashlib.sha256()
                with output.open("xb") as handle:
                    while chunk := source.read(1024 * 1024):
                        handle.write(chunk)
                        digest.update(chunk)
                executable = bool(member.mode & 0o111)
                os.chmod(output, 0o700 if executable else 0o600)
                inventory.append({
                    "path": str(relative),
                    "bytes": output.stat().st_size,
                    "sha256": digest.hexdigest(),
                    "executable": executable,
                })
        os.replace(staging, target)
        return sorted(inventory, key=lambda item: item["path"])
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
