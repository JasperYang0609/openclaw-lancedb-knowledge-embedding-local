#!/usr/bin/env python3
"""Safely acquire and release the directory lock shared by Qwen index/snapshot jobs."""
from __future__ import annotations

import argparse
import contextlib
import os
import stat
import sys
from pathlib import Path
from typing import Iterator


EXIT_UNSAFE = 74
EXIT_BUSY = 75


class UnsafeLock(RuntimeError):
    """The lock path or operation is not a safe contention state."""


@contextlib.contextmanager
def _open_parent(lock: Path) -> Iterator[tuple[int, str]]:
    absolute = Path(os.path.abspath(lock))
    if not absolute.is_absolute() or absolute == Path("/") or absolute.name != "index.lock":
        raise UnsafeLock("Index lock path is not absolute and specific")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise UnsafeLock("Secure index lock traversal is unsupported")
    descriptors: list[int] = []
    try:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for component in absolute.parent.parts[1:]:
            current = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            descriptors.append(current)
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise UnsafeLock("Index lock parent ownership or permissions are unsafe")
        yield current, absolute.name
    except OSError as error:
        raise UnsafeLock("Index lock parent is missing or unavailable") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_lock(metadata: os.stat_result, *, expected: tuple[int, int] | None = None) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeLock("Index lock identity or permissions are unsafe")
    if expected is not None and (metadata.st_dev, metadata.st_ino) != expected:
        raise UnsafeLock("Index lock identity changed")


def acquire(lock: Path) -> tuple[str, str | None]:
    """Return ``acquired`` plus identity or ``busy`` for one exact safe lock."""
    with _open_parent(lock) as (parent_fd, name):
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            try:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as error:
                raise UnsafeLock("Existing index lock cannot be verified") from error
            _validate_lock(metadata)
            return "busy", None
        except OSError as error:
            raise UnsafeLock("Index lock could not be created") from error

        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            _validate_lock(metadata)
            os.fsync(parent_fd)
        except Exception:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        return "acquired", f"{metadata.st_dev}:{metadata.st_ino}"


def _parse_identity(value: str) -> tuple[int, int]:
    pieces = value.split(":")
    if len(pieces) != 2 or any(not piece.isdigit() for piece in pieces):
        raise UnsafeLock("Index lock release identity is malformed")
    return int(pieces[0]), int(pieces[1])


def release(lock: Path, identity: str) -> None:
    expected = _parse_identity(identity)
    with _open_parent(lock) as (parent_fd, name):
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise UnsafeLock("Owned index lock is missing or unavailable") from error
        _validate_lock(metadata, expected=expected)
        try:
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise UnsafeLock("Owned index lock could not be released") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the fixed Qwen local index lock")
    parser.add_argument("command", choices=("acquire", "release"))
    parser.add_argument("--lock", required=True)
    parser.add_argument("--identity", default="")
    args = parser.parse_args(argv)
    try:
        lock = Path(args.lock)
        if args.command == "acquire":
            if args.identity:
                raise UnsafeLock("Acquire does not accept a release identity")
            status, identity = acquire(lock)
            if status == "busy":
                return EXIT_BUSY
            print(identity)
            return 0
        if not args.identity:
            raise UnsafeLock("Release requires the acquired lock identity")
        release(lock, args.identity)
        return 0
    except UnsafeLock:
        print("[index-lock] unsafe or unavailable index lock", file=sys.stderr)
        return EXIT_UNSAFE


if __name__ == "__main__":
    raise SystemExit(main())
