from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .artifacts import Artifact


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DownloadError(RuntimeError):
    pass


class ArtifactDownloader:
    def __init__(self, cache_dir: str | Path, *, curl: str | None = None) -> None:
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.curl = curl or shutil.which("curl")
        if not self.curl:
            raise DownloadError("system curl is required")

    @staticmethod
    def _verified(path: Path, artifact: Artifact) -> bool:
        return path.is_file() and path.stat().st_size == artifact.size and sha256_file(path) == artifact.sha256

    def _curl_args(self, artifact: Artifact, part: Path, *, resume: bool) -> list[str]:
        args = [
            self.curl,
            "--fail", "--location", "--silent", "--show-error",
            "--proto", "=https", "--proto-redir", "=https",
            "--tlsv1.2", "--retry", "4", "--retry-all-errors",
            "--connect-timeout", "20", "--max-time", "21600",
            "--output", str(part),
        ]
        if resume:
            args.extend(["--continue-at", "-"])
        args.append(artifact.url)
        return args

    def fetch(self, artifact: Artifact) -> Path:
        artifact.validate()
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.cache_dir / artifact.filename
        part = destination.with_suffix(destination.suffix + ".part")
        if destination.exists():
            if self._verified(destination, artifact):
                return destination
            quarantine = self.cache_dir / f"{artifact.filename}.quarantine-{int(time.time())}"
            os.replace(destination, quarantine)
        resume = part.is_file() and 0 < part.stat().st_size < artifact.size
        result = subprocess.run(self._curl_args(artifact, part, resume=resume), shell=False, check=False)
        if result.returncode == 33 and resume:
            part.unlink(missing_ok=True)
            result = subprocess.run(self._curl_args(artifact, part, resume=False), shell=False, check=False)
        if result.returncode != 0:
            raise DownloadError(f"{artifact.artifact_id} download failed (curl {result.returncode})")
        if not self._verified(part, artifact):
            raise DownloadError(f"{artifact.artifact_id} size or SHA-256 mismatch")
        os.replace(part, destination)
        return destination
