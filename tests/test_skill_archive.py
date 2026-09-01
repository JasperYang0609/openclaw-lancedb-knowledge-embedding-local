#!/usr/bin/env python3
from __future__ import annotations

import sys
import json
import subprocess
import tempfile
import warnings
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_skill_archive import OUTPUT, archive_manifest  # noqa: E402


def repack(source: Path, target: Path, mutate_first: bool = False) -> None:
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_STORED
    ) as rewritten:
        for index, info in enumerate(original.infolist()):
            data = original.read(info)
            if mutate_first and index == 0:
                data += b"\nchanged"
            replacement = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            replacement.compress_type = zipfile.ZIP_STORED
            replacement.external_attr = info.external_attr
            rewritten.writestr(replacement, data)



def write_single_member(path: Path, mode: int) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("member.txt", date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = mode << 16
        archive.writestr(info, b"same content")


def main() -> None:
    with zipfile.ZipFile(OUTPUT, "r") as packaged:
        names = set(packaged.namelist())
        prefix = "openclaw-lancedb-knowledge-local/"
        for required in (
            "qwen-local", "requirements.txt", "scripts/qwen_local.py",
            "src/installer/artifacts.py", "src/installer/downloader.py",
            "src/installer/safe_archive.py", "src/installer/qwen_installer.py",
            "src/lifecycle/llama_server_manager.py",
        ):
            assert prefix + required in names, f"missing packaged local runtime file: {required}"
    with tempfile.TemporaryDirectory(prefix="skill-archive-test-") as tmp_dir:
        tmp = Path(tmp_dir)
        extracted = tmp / "extracted"
        with zipfile.ZipFile(OUTPUT, "r") as packaged:
            packaged.extractall(extracted)
        installed = extracted / "openclaw-lancedb-knowledge-local"
        result = subprocess.run(
            [sys.executable, str(installed / "scripts/qwen_local.py"), "status", "--target",
             str(tmp / "managed/qwen")], capture_output=True, text=True, check=True,
        )
        status = json.loads(result.stdout)
        assert status["provider"] == "qwen-local" and status["installed"] is False
        differently_compressed = tmp / "stored.skill"
        repack(OUTPUT, differently_compressed)
        assert archive_manifest(OUTPUT) == archive_manifest(differently_compressed)

        changed = tmp / "changed.skill"
        repack(OUTPUT, changed, mutate_first=True)
        assert archive_manifest(OUTPUT) != archive_manifest(changed)

        duplicate = tmp / "duplicate.skill"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("duplicate.txt", b"first")
                archive.writestr("duplicate.txt", b"second")
        try:
            archive_manifest(duplicate)
        except ValueError as exc:
            assert "duplicate archive member" in str(exc)
        else:
            raise AssertionError("duplicate archive member must fail closed")

        mode_600 = tmp / "mode-600.skill"
        mode_644 = tmp / "mode-644.skill"
        mode_755 = tmp / "mode-755.skill"
        write_single_member(mode_600, 0o600)
        write_single_member(mode_644, 0o644)
        write_single_member(mode_755, 0o755)
        assert archive_manifest(mode_600) == archive_manifest(mode_644)
        assert archive_manifest(mode_644) != archive_manifest(mode_755)

    print("PASS test_skill_archive")


if __name__ == "__main__":
    main()
