from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from src.installer.artifacts import Artifact
from src.installer.downloader import ArtifactDownloader, DownloadError


def fixture_artifact(data: bytes) -> Artifact:
    return Artifact("fixture", "fixture.bin", "https://example.test/rev1/fixture.bin", len(data),
                    hashlib.sha256(data).hexdigest(), "rev1", "example.test")


def test_downloader_uses_argument_array_and_atomic_verified_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"verified fixture"
    artifact = fixture_artifact(data)

    def fake_run(args: list[str], **kwargs):
        assert kwargs["shell"] is False
        assert "--proto" in args and "=https" in args
        Path(args[args.index("--output") + 1]).write_bytes(data)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ArtifactDownloader(tmp_path, curl="/usr/bin/curl").fetch(artifact)
    assert result.read_bytes() == data
    assert not result.with_suffix(".bin.part").exists()


def test_resume_range_failure_restarts_without_appending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"complete fixture"
    artifact = fixture_artifact(data)
    part = tmp_path / "fixture.bin.part"
    part.write_bytes(b"partial")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 33)
        Path(args[args.index("--output") + 1]).write_bytes(data)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ArtifactDownloader(tmp_path, curl="/usr/bin/curl").fetch(artifact).read_bytes() == data
    assert "--continue-at" in calls[0]
    assert "--continue-at" not in calls[1]


def test_checksum_mismatch_fails_closed_and_keeps_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = fixture_artifact(b"expected")

    def fake_run(args: list[str], **kwargs):
        Path(args[args.index("--output") + 1]).write_bytes(b"tampered")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DownloadError, match="mismatch"):
        ArtifactDownloader(tmp_path, curl="/usr/bin/curl").fetch(artifact)
    assert (tmp_path / "fixture.bin.part").exists()
    assert not (tmp_path / "fixture.bin").exists()
