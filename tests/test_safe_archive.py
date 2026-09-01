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
                info.linkname = content.decode()
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


def test_secure_extract_materializes_safe_sibling_symlink_as_regular_file(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    entries = valid_entries() + [
        ("llama-b10625/libggml.0.22.0.dylib", b"verified dylib", "file"),
        ("llama-b10625/libggml.dylib", b"libggml.0.22.0.dylib", "symlink"),
    ]
    build_tar(archive, entries)
    inventory = extract_verified_tar(archive, tmp_path / "runtime")
    alias = tmp_path / "runtime/libggml.dylib"
    assert alias.is_file() and not alias.is_symlink()
    assert alias.read_bytes() == b"verified dylib"
    alias_item = next(item for item in inventory if item["path"] == "libggml.dylib")
    assert alias_item["materializedFrom"] == "libggml.0.22.0.dylib"


def test_secure_extract_materializes_bounded_sibling_symlink_chain(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    entries = valid_entries() + [
        ("llama-b10625/libggml.0.22.0.dylib", b"verified dylib", "file"),
        ("llama-b10625/libggml.0.dylib", b"libggml.0.22.0.dylib", "symlink"),
        ("llama-b10625/libggml.dylib", b"libggml.0.dylib", "symlink"),
    ]
    build_tar(archive, entries)
    inventory = extract_verified_tar(archive, tmp_path / "runtime")
    alias = tmp_path / "runtime/libggml.dylib"
    assert alias.is_file() and not alias.is_symlink()
    assert alias.read_bytes() == b"verified dylib"
    alias_item = next(item for item in inventory if item["path"] == "libggml.dylib")
    assert alias_item["archiveLinkChain"] == ["libggml.0.dylib", "libggml.0.22.0.dylib"]


@pytest.mark.parametrize("entry,content,kind", [("llama-b10625/../../escape", b"bad", "file"),
                                                  ("/llama-b10625/bin/llama-server", b"bad", "file"),
                                                  ("llama-b10625/link", b"/tmp/outside", "symlink"),
                                                  ("llama-b10625/link", b"../outside", "symlink"),
                                                  ("llama-b10625/link", b"missing.dylib", "symlink"),
                                                  ("llama-b10625/link", b"link", "symlink")])
def test_secure_extract_rejects_malicious_entries(
    tmp_path: Path, entry: str, content: bytes, kind: str
) -> None:
    archive = tmp_path / "bad.tar.gz"
    build_tar(archive, valid_entries() + [(entry, content, kind)])
    with pytest.raises(UnsafeArchiveError):
        extract_verified_tar(archive, tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()
