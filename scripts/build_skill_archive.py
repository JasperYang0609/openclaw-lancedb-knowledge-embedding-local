#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "openclaw-lancedb-knowledge-local"
OUTPUT = ROOT / "dist" / "openclaw-lancedb-knowledge-local.skill"
EXCLUDED_PARTS = {"node_modules", "__pycache__", "data", "reports"}
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def normalized_archive_mode(mode: int) -> int:
    """Preserve executable semantics while ignoring checkout umask differences."""
    return 0o755 if mode & 0o111 else 0o644


def source_files() -> list[Path]:
    skill_files = sorted(
        path
        for path in SOURCE.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.relative_to(SOURCE).parts)
        and path.suffix != ".pyc"
    )
    runtime_files = [
        ROOT / "LICENSE",
        ROOT / "qwen-local",
        ROOT / "requirements.txt",
        ROOT / "scripts/qwen_local.py",
        *sorted((ROOT / "src/installer").glob("*.py")),
        *sorted((ROOT / "src/lifecycle").glob("*.py")),
        *sorted((ROOT / "src/openclaw_integration").glob("*.py")),
        ROOT / "plugin/openclaw-lancedb-knowledge-local/README.md",
        ROOT / "plugin/openclaw-lancedb-knowledge-local/index.js",
        ROOT / "plugin/openclaw-lancedb-knowledge-local/openclaw.plugin.json",
        ROOT / "plugin/openclaw-lancedb-knowledge-local/package.json",
        ROOT / "plugin/openclaw-lancedb-knowledge-local/package-lock.json",
        ROOT / "plugin/openclaw-lancedb-knowledge-local/src/search-tool.js",
    ]
    return skill_files + [path for path in runtime_files if path.is_file()]


def archive_name(path: Path) -> Path:
    if path.is_relative_to(SOURCE):
        return Path(SOURCE.name) / path.relative_to(SOURCE)
    return Path(SOURCE.name) / path.relative_to(ROOT)


def build(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in source_files():
            info = zipfile.ZipInfo(str(archive_name(path)), date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = normalized_archive_mode(path.stat().st_mode) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)



def archive_manifest(path: Path) -> dict[str, tuple[bytes, int]]:
    """Compare logical archive content, independent of zlib/Python output bytes."""
    with zipfile.ZipFile(path, "r") as archive:
        manifest: dict[str, tuple[bytes, int]] = {}
        for info in archive.infolist():
            if info.filename in manifest:
                raise ValueError(f"duplicate archive member: {info.filename}")
            mode = normalized_archive_mode(info.external_attr >> 16)
            manifest[info.filename] = (archive.read(info), mode)
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or verify the deterministic .skill archive")
    parser.add_argument("--check", action="store_true", help="Fail when dist artifact differs from source")
    args = parser.parse_args()

    if args.check:
        with tempfile.TemporaryDirectory(prefix="skill-archive-check-") as tmp_dir:
            candidate = Path(tmp_dir) / OUTPUT.name
            build(candidate)
            if not OUTPUT.exists() or archive_manifest(candidate) != archive_manifest(OUTPUT):
                raise SystemExit("dist/openclaw-lancedb-knowledge-local.skill is stale; rebuild it")
        print(f"PASS skill archive matches {len(source_files())} source files")
        return

    with tempfile.NamedTemporaryFile(dir=OUTPUT.parent, prefix=".skill-", delete=False) as handle:
        candidate = Path(handle.name)
    try:
        build(candidate)
        os.replace(candidate, OUTPUT)
    finally:
        candidate.unlink(missing_ok=True)
    print(f"built {OUTPUT} with {len(source_files())} files")


if __name__ == "__main__":
    main()
