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
    if member.islnk() or member.isdev() or member.isfifo():
        raise UnsafeArchiveError(f"unsupported archive entry: {member.name}")
    if not (member.isfile() or member.isdir() or member.issym()):
        raise UnsafeArchiveError(f"unknown archive entry type: {member.name}")
    return name


def _safe_sibling_link(member: tarfile.TarInfo, name: PurePosixPath) -> PurePosixPath:
    """Resolve an archive symlink only when it names a regular sibling member.

    The pinned llama.cpp macOS release uses sibling dylib aliases. We never
    install archive symlinks: after validating the whole archive, each alias is
    materialized as a regular copy of its verified target.
    """
    link = PurePosixPath(member.linkname)
    if link.is_absolute() or len(link.parts) != 1 or link.parts[0] in {"", ".", ".."}:
        raise UnsafeArchiveError(f"unsafe archive symlink target: {member.name}")
    return name.parent / link


def _terminal_regular_link(
    member: tarfile.TarInfo,
    name: PurePosixPath,
    regular_members: dict[PurePosixPath, tarfile.TarInfo],
    symlink_members: dict[PurePosixPath, tarfile.TarInfo],
    *,
    max_depth: int = 8,
) -> tuple[PurePosixPath, list[PurePosixPath]]:
    current_member = member
    current_name = name
    visited = {name}
    chain: list[PurePosixPath] = []
    for _ in range(max_depth):
        target_name = _safe_sibling_link(current_member, current_name)
        chain.append(target_name)
        if target_name in regular_members:
            return target_name, chain
        if target_name in visited:
            raise UnsafeArchiveError(f"archive symlink chain contains a loop: {member.name}")
        next_member = symlink_members.get(target_name)
        if next_member is None:
            raise UnsafeArchiveError(f"archive symlink target is not a regular member: {member.name}")
        visited.add(target_name)
        current_name = target_name
        current_member = next_member
    raise UnsafeArchiveError(f"archive symlink chain is too deep: {member.name}")


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
            regular_members = {name: member for member, name in checked if member.isfile()}
            symlink_members = {name: member for member, name in checked if member.issym()}
            regular_names = {str(name) for name in regular_members}
            if not any(name.endswith("/llama-server") for name in regular_names):
                raise UnsafeArchiveError("archive is missing llama-server")
            if not any(PurePosixPath(name).name.upper().startswith("LICENSE") for name in regular_names):
                raise UnsafeArchiveError("archive is missing LICENSE")
            for member, name in checked:
                if member.issym():
                    continue
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
            for member, name in checked:
                if not member.issym():
                    continue
                target_name, link_chain = _terminal_regular_link(
                    member, name, regular_members, symlink_members
                )
                target_member = regular_members[target_name]
                relative = Path(*name.parts[1:])
                target_relative = Path(*target_name.parts[1:])
                source_path = staging / target_relative
                output = staging / relative
                if not source_path.is_file() or source_path.is_symlink():
                    raise UnsafeArchiveError(f"archive symlink target is unsafe: {member.name}")
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source_path, output)
                executable = bool(target_member.mode & 0o111)
                os.chmod(output, 0o700 if executable else 0o600)
                digest = hashlib.sha256(output.read_bytes()).hexdigest()
                inventory.append({
                    "path": str(relative),
                    "bytes": output.stat().st_size,
                    "sha256": digest,
                    "executable": executable,
                    "materializedFrom": str(target_relative),
                    "archiveLinkChain": [str(Path(*item.parts[1:])) for item in link_chain],
                })
        os.replace(staging, target)
        return sorted(inventory, key=lambda item: item["path"])
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
