from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from src.installer.artifacts import Artifact
from src.installer.downloader import ArtifactDownloader, DownloadError, EFFECTIVE_URL_MARKER


def fixture_artifact(data: bytes) -> Artifact:
    return Artifact("fixture", "fixture.bin", "https://example.test/rev1/fixture.bin", len(data),
                    hashlib.sha256(data).hexdigest(), "rev1", "example.test")


def successful(args: list[str], artifact: Artifact, *, effective_url: str | None = None):
    stderr = f"{EFFECTIVE_URL_MARKER}{effective_url or artifact.url}".encode()
    return subprocess.CompletedProcess(args, 0, stderr=stderr)


def test_downloader_uses_argument_array_and_atomic_verified_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"verified fixture"
    artifact = fixture_artifact(data)

    def fake_run(args: list[str], **kwargs):
        assert kwargs["shell"] is False
        assert "--proto" in args and "=https" in args
        assert args[args.index("--output") + 1] == "-"
        kwargs["stdout"].write(data)
        return successful(args, artifact)

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
        kwargs["stdout"].write(data)
        return successful(args, artifact)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ArtifactDownloader(tmp_path, curl="/usr/bin/curl").fetch(artifact).read_bytes() == data
    assert "--continue-at" in calls[0]
    assert "--continue-at" not in calls[1]


def test_checksum_mismatch_fails_closed_and_keeps_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = fixture_artifact(b"expected")

    def fake_run(args: list[str], **kwargs):
        kwargs["stdout"].write(b"tampered")
        return successful(args, artifact)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DownloadError, match="mismatch"):
        ArtifactDownloader(tmp_path, curl="/usr/bin/curl").fetch(artifact)
    assert (tmp_path / "fixture.bin.part").exists()
    assert not (tmp_path / "fixture.bin").exists()


@pytest.mark.parametrize("plant", ["symlink", "hardlink"])
def test_downloader_refuses_preplanted_partial_write_target(tmp_path: Path, plant: str) -> None:
    artifact = fixture_artifact(b"expected")
    outside = tmp_path / "outside-user-file"
    outside.write_bytes(b"do-not-overwrite")
    part = tmp_path / "fixture.bin.part"
    if plant == "symlink":
        part.symlink_to(outside)
    else:
        os.link(outside, part)

    with pytest.raises(DownloadError, match="partial"):
        ArtifactDownloader(tmp_path, curl="/usr/bin/curl").fetch(artifact)

    assert outside.read_bytes() == b"do-not-overwrite"


def test_downloader_rejects_redirect_outside_approved_https_hosts(tmp_path: Path, monkeypatch) -> None:
    data = b"verified"
    artifact = fixture_artifact(data)

    def fake_run(args: list[str], **kwargs):
        kwargs["stdout"].write(data)
        return successful(args, artifact, effective_url="https://attacker.example/artifact.bin")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DownloadError, match="approved HTTPS hosts"):
        ArtifactDownloader(tmp_path, curl="/usr/bin/curl").fetch(artifact)


def test_downloader_accepts_explicit_official_redirect_host(tmp_path: Path, monkeypatch) -> None:
    data = b"verified"
    artifact = Artifact(
        artifact_id="fixture-r1",
        filename="fixture.bin",
        url="https://downloads.example/r1/fixture.bin",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        revision="r1",
        allowed_host="downloads.example",
        allowed_redirect_hosts=("cdn.example",),
    )

    def fake_run(args: list[str], **kwargs):
        kwargs["stdout"].write(data)
        return successful(args, artifact, effective_url="https://cdn.example/immutable/object")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ArtifactDownloader(tmp_path, curl="/usr/bin/curl").fetch(artifact)
    assert result.read_bytes() == data
