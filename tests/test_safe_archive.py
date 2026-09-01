from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from src.installer.safe_archive import UnsafeArchiveError, extract_verified_tar


def build_tar(path: Path, entries: list[tuple[str, bytes, str]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/tmp/outside"
                archive.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o755 if name.endswith("llama-server") else 0o644
                archive.addfile(info, io.BytesIO(content))


def valid_entries() -> list[tuple[str, bytes, str]]:
    return [("llama-b10625/bin/llama-server", b"runtime", "file"),
            ("llama-b10625/LICENSE", b"MIT", "file")]


def test_secure_extract_inventory_and_permissions(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    build_tar(archive, valid_entries())
    inventory = extract_verified_tar(archive, tmp_path / "runtime")
    assert {item["path"] for item in inventory} == {"bin/llama-server", "LICENSE"}
    assert (tmp_path / "runtime/bin/llama-server").stat().st_mode & 0o111


@pytest.mark.parametrize("entry,kind", [("llama-b10625/../../escape", "file"),
                                          ("/llama-b10625/bin/llama-server", "file"),
                                          ("llama-b10625/link", "symlink")])
def test_secure_extract_rejects_malicious_entries(tmp_path: Path, entry: str, kind: str) -> None:
    archive = tmp_path / "bad.tar.gz"
    build_tar(archive, valid_entries() + [(entry, b"bad", kind)])
    with pytest.raises(UnsafeArchiveError):
        extract_verified_tar(archive, tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()
