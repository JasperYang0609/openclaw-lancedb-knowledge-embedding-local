from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .artifacts import Artifact


EFFECTIVE_URL_MARKER = "__QWEN_LOCAL_EFFECTIVE_URL__="


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
        self.cache_dir = Path(os.path.abspath(Path(cache_dir).expanduser()))
        for component in (self.cache_dir, *self.cache_dir.parents):
            if component.is_symlink():
                raise DownloadError("artifact cache path must not contain symbolic links")
        self.curl = curl or shutil.which("curl")
        if not self.curl:
            raise DownloadError("system curl is required")

    @staticmethod
    def _verified(path: Path, artifact: Artifact) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        metadata = path.stat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_size == artifact.size
            and sha256_file(path) == artifact.sha256
        )

    def _curl_args(self, artifact: Artifact, *, resume_offset: int = 0) -> list[str]:
        args = [
            self.curl,
            "--fail", "--location", "--silent", "--show-error",
            "--proto", "=https", "--proto-redir", "=https",
            "--tlsv1.2", "--retry", "4", "--retry-all-errors",
            "--connect-timeout", "20", "--max-time", "21600",
            "--max-filesize", str(artifact.size),
            "--output", "-",
            "--write-out", f"%{{stderr}}{EFFECTIVE_URL_MARKER}%{{url_effective}}",
        ]
        if resume_offset:
            args.extend(["--continue-at", str(resume_offset)])
        args.append(artifact.url)
        return args

    @staticmethod
    def _safe_part_metadata(path: Path) -> os.stat_result:
        if path.is_symlink() or not path.is_file():
            raise DownloadError("artifact partial path must be a regular file")
        metadata = path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or not stat.S_ISREG(metadata.st_mode):
            raise DownloadError("artifact partial path has unsafe ownership or link count")
        return metadata

    @staticmethod
    def _open_part(path: Path, *, append: bool) -> tuple[object, os.stat_result]:
        flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= os.O_APPEND if append else os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except (FileExistsError, OSError) as error:
            raise DownloadError("artifact partial path already exists or is unsafe") from error
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise DownloadError("artifact partial file descriptor is unsafe")
        return os.fdopen(descriptor, "ab" if append else "wb", buffering=0), metadata

    @staticmethod
    def _unlink_same_file(path: Path, expected: os.stat_result) -> None:
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise DownloadError("artifact partial path changed during download")
        path.unlink()

    @staticmethod
    def _validate_effective_url(artifact: Artifact, stderr: bytes | None) -> None:
        try:
            diagnostic = (stderr or b"").decode("utf-8", errors="strict")
            effective_url = diagnostic.rsplit(EFFECTIVE_URL_MARKER, 1)[1].strip()
        except (UnicodeDecodeError, IndexError) as error:
            raise DownloadError("curl did not report a valid effective artifact URL") from error
        parsed = urlparse(effective_url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or not artifact.allows_download_host(parsed.hostname)
        ):
            raise DownloadError("artifact redirect resolved outside the approved HTTPS hosts")

    def _run_download(self, artifact: Artifact, part: Path, *, resume_offset: int) -> int:
        output, opened = self._open_part(part, append=bool(resume_offset))
        try:
            result = subprocess.run(
                self._curl_args(artifact, resume_offset=resume_offset),
                shell=False,
                check=False,
                stdout=output,
                stderr=subprocess.PIPE,
            )
        finally:
            output.close()
        if not part.exists() or self._safe_part_metadata(part).st_ino != opened.st_ino:
            raise DownloadError("artifact partial path changed during download")
        if result.returncode == 0:
            self._validate_effective_url(artifact, result.stderr)
        return result.returncode

    def fetch(self, artifact: Artifact) -> Path:
        artifact.validate()
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.cache_dir / artifact.filename
        part = destination.with_suffix(destination.suffix + ".part")
        if destination.exists() or destination.is_symlink():
            if self._verified(destination, artifact):
                return destination
            if destination.is_symlink() or not destination.is_file():
                raise DownloadError("artifact destination is not a safe regular file")
            metadata = destination.stat()
            if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
                raise DownloadError("artifact destination has unsafe ownership or link count")
            quarantine = self.cache_dir / f"{artifact.filename}.quarantine-{int(time.time())}-{secrets.token_hex(8)}"
            os.replace(destination, quarantine)
        resume_offset = 0
        if part.exists() or part.is_symlink():
            metadata = self._safe_part_metadata(part)
            if 0 < metadata.st_size < artifact.size:
                resume_offset = metadata.st_size
            else:
                self._unlink_same_file(part, metadata)
        returncode = self._run_download(artifact, part, resume_offset=resume_offset)
        if returncode == 33 and resume_offset:
            metadata = self._safe_part_metadata(part)
            self._unlink_same_file(part, metadata)
            returncode = self._run_download(artifact, part, resume_offset=0)
        if returncode != 0:
            raise DownloadError(f"{artifact.artifact_id} download failed (curl {returncode})")
        if not self._verified(part, artifact):
            raise DownloadError(f"{artifact.artifact_id} size or SHA-256 mismatch")
        os.replace(part, destination)
        return destination
