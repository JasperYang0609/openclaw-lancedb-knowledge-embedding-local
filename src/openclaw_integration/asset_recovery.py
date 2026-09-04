from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Callable


SymlinkPolicy = Callable[[str, str], None]
RenameNoReplace = Callable[[int, str, int, str], None]


@dataclass(frozen=True)
class AssetIdentity:
    kind: str
    dev: int
    ino: int
    mode: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "dev": self.dev,
            "ino": self.ino,
            "mode": self.mode,
            "sha256": self.sha256,
        }


def _safe_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise RuntimeError("Rollback asset name is unsafe")


def _validate_owned(metadata: os.stat_result, *, kind: str) -> None:
    if metadata.st_uid != os.getuid() or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("Rollback asset ownership or permissions are unsafe")
    if kind == "directory":
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("Rollback asset kind is unsafe")
    elif kind == "file":
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("Rollback asset kind or link count is unsafe")
    else:
        raise RuntimeError("Rollback asset kind is unsupported")


def _stable(before: os.stat_result, after: os.stat_result) -> None:
    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
        if getattr(before, field) != getattr(after, field):
            raise RuntimeError("Rollback asset changed while it was being read")


def _digest_file_at(parent_fd: int, name: str, metadata: os.stat_result) -> str:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        _validate_owned(opened, kind="file")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError("Rollback asset identity changed before read")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            before = os.fstat(handle.fileno())
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        _stable(before, after)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError("Rollback asset identity changed after read")
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _digest_directory_fd(
    directory_fd: int,
    *,
    prefix: str,
    digest: "hashlib._Hash",
    symlink_policy: SymlinkPolicy | None,
) -> None:
    for name in sorted(os.listdir(directory_fd)):
        _safe_name(name)
        relative = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if metadata.st_uid != os.getuid() or (
            not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Rollback asset tree contains unsafe ownership or permissions")
        mode = str(stat.S_IMODE(metadata.st_mode)).encode("ascii")
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(b"D\0" + relative.encode("utf-8") + b"\0" + mode + b"\0")
            child_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                _validate_owned(opened, kind="directory")
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise RuntimeError("Rollback asset directory identity changed")
                _digest_directory_fd(
                    child_fd, prefix=relative, digest=digest, symlink_policy=symlink_policy,
                )
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise RuntimeError("Rollback asset directory identity changed")
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise RuntimeError("Rollback asset tree contains a multiply linked file")
            content = _digest_file_at(directory_fd, name, metadata)
            digest.update(
                b"F\0" + relative.encode("utf-8") + b"\0" + mode + b"\0"
                + content.encode("ascii") + b"\0"
            )
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(name, dir_fd=directory_fd)
            if symlink_policy is None:
                raise RuntimeError("Rollback asset tree contains an unsupported symbolic link")
            symlink_policy(relative, target)
            digest.update(
                b"L\0" + relative.encode("utf-8") + b"\0"
                + target.encode("utf-8") + b"\0"
            )
        else:
            raise RuntimeError("Rollback asset tree contains a special node")


def inspect_asset_at(
    parent_fd: int,
    name: str,
    *,
    kind: str,
    symlink_policy: SymlinkPolicy | None = None,
) -> AssetIdentity:
    _safe_name(name)
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _validate_owned(metadata, kind=kind)
    if kind == "file":
        value = _digest_file_at(parent_fd, name, metadata)
    else:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            _validate_owned(opened, kind="directory")
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeError("Rollback asset root identity changed")
            digest = hashlib.sha256()
            digest.update(
                b"ROOT\0directory\0" + str(stat.S_IMODE(opened.st_mode)).encode("ascii") + b"\0"
            )
            _digest_directory_fd(
                descriptor, prefix="", digest=digest, symlink_policy=symlink_policy,
            )
            value = digest.hexdigest()
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeError("Rollback asset root identity changed")
        finally:
            os.close(descriptor)
    return AssetIdentity(
        kind=kind,
        dev=metadata.st_dev,
        ino=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        sha256=value,
    )


def _copy_file_at(
    source_fd: int, source_name: str, target_fd: int, target_name: str,
    metadata: os.stat_result,
) -> None:
    source = os.open(
        source_name,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
        dir_fd=source_fd,
    )
    target = -1
    try:
        opened = os.fstat(source)
        _validate_owned(opened, kind="file")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError("Rollback backup identity changed before copy")
        target = os.open(
            target_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(metadata.st_mode),
            dir_fd=target_fd,
        )
        with os.fdopen(source, "rb", closefd=True) as input_handle, \
                os.fdopen(target, "wb", closefd=True) as output_handle:
            source = -1
            target = -1
            before = os.fstat(input_handle.fileno())
            for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                output_handle.write(chunk)
            after = os.fstat(input_handle.fileno())
            _stable(before, after)
            os.fchmod(output_handle.fileno(), stat.S_IMODE(metadata.st_mode))
            output_handle.flush()
            os.fsync(output_handle.fileno())
    finally:
        if source >= 0:
            os.close(source)
        if target >= 0:
            os.close(target)


def _copy_directory_fd(
    source_fd: int,
    target_fd: int,
    *,
    prefix: str,
    symlink_policy: SymlinkPolicy | None,
) -> None:
    for name in sorted(os.listdir(source_fd)):
        _safe_name(name)
        relative = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if metadata.st_uid != os.getuid() or (
            not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Rollback backup contains unsafe ownership or permissions")
        if stat.S_ISDIR(metadata.st_mode):
            os.mkdir(name, stat.S_IMODE(metadata.st_mode), dir_fd=target_fd)
            source_child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source_fd,
            )
            target_child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target_fd,
            )
            try:
                opened = os.fstat(source_child)
                _validate_owned(opened, kind="directory")
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise RuntimeError("Rollback backup directory changed before copy")
                os.fchmod(target_child, stat.S_IMODE(metadata.st_mode))
                _copy_directory_fd(
                    source_child, target_child, prefix=relative, symlink_policy=symlink_policy,
                )
                os.fsync(target_child)
            finally:
                os.close(target_child)
                os.close(source_child)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise RuntimeError("Rollback backup contains a multiply linked file")
            _copy_file_at(source_fd, name, target_fd, name, metadata)
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(name, dir_fd=source_fd)
            if symlink_policy is None:
                raise RuntimeError("Rollback backup contains an unsupported symbolic link")
            symlink_policy(relative, target)
            os.symlink(target, name, dir_fd=target_fd)
        else:
            raise RuntimeError("Rollback backup contains a special node")


def stage_copy_at(
    backup_parent_fd: int,
    backup_name: str,
    target_parent_fd: int,
    stage_name: str,
    *,
    expected_backup: AssetIdentity,
    symlink_policy: SymlinkPolicy | None = None,
) -> AssetIdentity:
    _safe_name(backup_name)
    _safe_name(stage_name)
    current = inspect_asset_at(
        backup_parent_fd,
        backup_name,
        kind=expected_backup.kind,
        symlink_policy=symlink_policy,
    )
    if current != expected_backup:
        raise RuntimeError("Rollback backup identity or digest changed")
    created = False
    if expected_backup.kind == "directory":
        os.mkdir(stage_name, expected_backup.mode, dir_fd=target_parent_fd)
        created = True
        source = os.open(
            backup_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=backup_parent_fd,
        )
        target = os.open(
            stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=target_parent_fd,
        )
        try:
            source_root = os.fstat(source)
            _validate_owned(source_root, kind="directory")
            if (source_root.st_dev, source_root.st_ino) != (
                expected_backup.dev, expected_backup.ino,
            ):
                raise RuntimeError("Rollback backup root identity changed before copy")
            os.fchmod(target, expected_backup.mode)
            _copy_directory_fd(
                source, target, prefix="", symlink_policy=symlink_policy,
            )
            os.fsync(target)
        finally:
            os.close(target)
            os.close(source)
    else:
        metadata = os.stat(backup_name, dir_fd=backup_parent_fd, follow_symlinks=False)
        _copy_file_at(
            backup_parent_fd, backup_name, target_parent_fd, stage_name, metadata,
        )
        created = True
    if not created:
        raise RuntimeError("Rollback restore stage was not created")
    os.fsync(target_parent_fd)
    source_after = inspect_asset_at(
        backup_parent_fd,
        backup_name,
        kind=expected_backup.kind,
        symlink_policy=symlink_policy,
    )
    if source_after != expected_backup:
        raise RuntimeError("Rollback backup changed while it was being copied")
    staged = inspect_asset_at(
        target_parent_fd,
        stage_name,
        kind=expected_backup.kind,
        symlink_policy=symlink_policy,
    )
    if staged.kind != expected_backup.kind or staged.mode != expected_backup.mode \
            or staged.sha256 != expected_backup.sha256:
        raise RuntimeError("Rollback restore stage verification failed")
    return staged


def quarantine_exact_at(
    parent_fd: int,
    name: str,
    quarantine_name: str,
    *,
    expected: AssetIdentity,
    rename_noreplace: RenameNoReplace,
    symlink_policy: SymlinkPolicy | None = None,
) -> bool:
    _safe_name(name)
    _safe_name(quarantine_name)
    try:
        current = inspect_asset_at(
            parent_fd, name, kind=expected.kind, symlink_policy=symlink_policy,
        )
    except FileNotFoundError:
        try:
            moved = inspect_asset_at(
                parent_fd, quarantine_name, kind=expected.kind,
                symlink_policy=symlink_policy,
            )
        except FileNotFoundError:
            return False
        if moved != expected:
            raise RuntimeError("Rollback quarantine identity changed")
        return True
    if current != expected:
        raise RuntimeError("Rollback target identity or digest changed")
    rename_noreplace(parent_fd, name, parent_fd, quarantine_name)
    os.fsync(parent_fd)
    moved = inspect_asset_at(
        parent_fd, quarantine_name, kind=expected.kind, symlink_policy=symlink_policy,
    )
    if moved != expected:
        raise RuntimeError("Rollback target changed during quarantine")
    return True


def publish_noreplace_at(
    parent_fd: int,
    stage_name: str,
    final_name: str,
    *,
    expected_stage: AssetIdentity,
    rename_noreplace: RenameNoReplace,
    symlink_policy: SymlinkPolicy | None = None,
) -> AssetIdentity:
    _safe_name(stage_name)
    _safe_name(final_name)
    try:
        final = inspect_asset_at(
            parent_fd, final_name, kind=expected_stage.kind, symlink_policy=symlink_policy,
        )
    except FileNotFoundError:
        stage = inspect_asset_at(
            parent_fd, stage_name, kind=expected_stage.kind, symlink_policy=symlink_policy,
        )
        if stage != expected_stage:
            raise RuntimeError("Rollback restore stage identity changed")
        rename_noreplace(parent_fd, stage_name, parent_fd, final_name)
        os.fsync(parent_fd)
        final = inspect_asset_at(
            parent_fd, final_name, kind=expected_stage.kind, symlink_policy=symlink_policy,
        )
    if final != expected_stage:
        raise RuntimeError("Rollback restored asset identity or digest changed")
    return final


def _purge_tree_fd(directory_fd: int) -> None:
    for name in sorted(os.listdir(directory_fd)):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if metadata.st_uid != os.getuid():
            raise RuntimeError("Rollback quarantine child ownership changed")
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd,
            )
            try:
                if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (
                    metadata.st_dev, metadata.st_ino,
                ):
                    raise RuntimeError("Rollback quarantine child identity changed")
                _purge_tree_fd(child)
            finally:
                os.close(child)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeError("Rollback quarantine child identity changed")
            os.rmdir(name, dir_fd=directory_fd)
        else:
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise RuntimeError("Rollback quarantine contains a multiply linked file")
            if not stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError("Rollback quarantine contains a special node")
            os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def purge_exact_at(
    parent_fd: int,
    quarantine_name: str,
    *,
    expected: AssetIdentity,
    symlink_policy: SymlinkPolicy | None = None,
) -> bool:
    _safe_name(quarantine_name)
    try:
        current = inspect_asset_at(
            parent_fd, quarantine_name, kind=expected.kind, symlink_policy=symlink_policy,
        )
    except FileNotFoundError:
        return False
    if current != expected:
        raise RuntimeError("Rollback quarantine identity or digest changed before purge")
    if expected.kind == "directory":
        descriptor = os.open(
            quarantine_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (expected.dev, expected.ino):
                raise RuntimeError("Rollback quarantine identity changed before purge")
            _purge_tree_fd(descriptor)
        finally:
            os.close(descriptor)
        current_meta = os.stat(
            quarantine_name, dir_fd=parent_fd, follow_symlinks=False,
        )
        if (current_meta.st_dev, current_meta.st_ino) != (expected.dev, expected.ino):
            raise RuntimeError("Rollback quarantine identity changed before purge")
        os.rmdir(quarantine_name, dir_fd=parent_fd)
    else:
        current_meta = os.stat(
            quarantine_name, dir_fd=parent_fd, follow_symlinks=False,
        )
        if (current_meta.st_dev, current_meta.st_ino) != (expected.dev, expected.ino):
            raise RuntimeError("Rollback quarantine identity changed before purge")
        os.unlink(quarantine_name, dir_fd=parent_fd)
    os.fsync(parent_fd)
    return True
