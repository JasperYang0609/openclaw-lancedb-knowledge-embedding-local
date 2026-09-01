from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import stat
from pathlib import Path


QWEN_Q5_SHA256 = "9fd05563211c2d69d74abb8769fa92983a102d11575b2517a119b0037dff217c"
LLAMA_SERVER_SHA256 = "9c1aa07bd394a2472d1a9373bdb09a09485016410cbac724598b0b385eefa588"
QWEN_REVISION = "f4602530db1d980e16da9d7d3a70294cf5c190be"
LLAMA_CPP_REVISION = "f1357e49980f5462af9783164f3fdec407d90137"


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json_write(file_path: Path, payload: dict) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(file_path)


class QwenInstaller:
    """Install verified local artifacts inside one explicitly managed root."""

    def __init__(
        self,
        target_dir: str | Path,
        *,
        model_sha256: str = QWEN_Q5_SHA256,
        server_sha256: str = LLAMA_SERVER_SHA256,
    ) -> None:
        self.target_dir = Path(target_dir).expanduser().resolve()
        if self.target_dir == Path(self.target_dir.anchor) or len(self.target_dir.parts) < 4:
            raise ValueError("Installer target must be a specific managed directory")
        self.model_sha256 = model_sha256
        self.server_sha256 = server_sha256
        self.model_path = self.target_dir / "models" / "Qwen3-Embedding-4B-Q5_K_M.gguf"
        self.server_path = self.target_dir / "bin" / "llama-server"
        self.api_key_file = self.target_dir / "run" / "api-key"
        self.manifest_path = self.target_dir / "install-manifest.json"

    @staticmethod
    def system_preflight(*, minimum_ram_gib: int = 16, minimum_free_gib: int = 12) -> dict:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("This validation installer currently supports macOS Apple Silicon only")
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
        ram_bytes = page_size * page_count
        free_bytes = shutil.disk_usage(Path.home()).free
        if ram_bytes < minimum_ram_gib * 1024**3:
            raise RuntimeError(f"At least {minimum_ram_gib} GiB RAM is required")
        if free_bytes < minimum_free_gib * 1024**3:
            raise RuntimeError(f"At least {minimum_free_gib} GiB free disk is required")
        return {
            "platform": "darwin-arm64",
            "ramBytes": ram_bytes,
            "freeBytes": free_bytes,
        }

    @staticmethod
    def _verify(source: Path, expected_sha256: str, label: str) -> None:
        if not source.is_file():
            raise FileNotFoundError(f"Verified {label} source is missing")
        actual = sha256_file(source)
        if actual != expected_sha256:
            raise RuntimeError(f"{label} SHA-256 mismatch")

    def install_from_verified_sources(
        self,
        *,
        model_source: str | Path,
        server_source: str | Path,
        development_model_link: bool = True,
    ) -> dict:
        model_source_path = Path(model_source).expanduser().resolve()
        server_source_path = Path(server_source).expanduser().resolve()
        self._verify(model_source_path, self.model_sha256, "Qwen model")
        self._verify(server_source_path, self.server_sha256, "llama-server")
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        self.server_path.parent.mkdir(parents=True, exist_ok=True)
        self.api_key_file.parent.mkdir(parents=True, exist_ok=True)
        for directory in (self.target_dir, self.model_path.parent, self.server_path.parent, self.api_key_file.parent):
            os.chmod(directory, stat.S_IRWXU)

        if self.model_path.exists() or self.model_path.is_symlink():
            if self.model_path.is_symlink() and self.model_path.resolve() == model_source_path:
                pass
            elif sha256_file(self.model_path) != self.model_sha256:
                raise RuntimeError("Managed model path exists with the wrong SHA-256")
        elif development_model_link:
            self.model_path.symlink_to(model_source_path)
        else:
            shutil.copy2(model_source_path, self.model_path)

        if self.server_path.is_symlink():
            raise RuntimeError("Managed llama-server path must not be a symbolic link")
        if not self.server_path.exists():
            shutil.copy2(server_source_path, self.server_path)
        self._verify(self.model_path, self.model_sha256, "installed Qwen model")
        self._verify(self.server_path, self.server_sha256, "installed llama-server")
        os.chmod(self.server_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        if self.api_key_file.is_symlink():
            raise RuntimeError("Local API credential must not be a symbolic link")
        if not self.api_key_file.exists():
            descriptor = os.open(self.api_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(secrets.token_urlsafe(48) + "\n")
        os.chmod(self.api_key_file, stat.S_IRUSR | stat.S_IWUSR)
        manifest = {
            "schemaVersion": 1,
            "provider": "qwen-local",
            "model": "Qwen3-Embedding-4B-Q5_K_M",
            "modelRevision": QWEN_REVISION,
            "modelSha256": self.model_sha256,
            "runtimeRevision": LLAMA_CPP_REVISION,
            "runtimeSha256": self.server_sha256,
            "modelPath": str(self.model_path),
            "serverPath": str(self.server_path),
            "apiKeyFile": str(self.api_key_file),
            "developmentModelLink": bool(development_model_link),
        }
        atomic_json_write(self.manifest_path, manifest)
        return manifest

    def verify_installation(self) -> dict:
        if not self.manifest_path.is_file():
            raise RuntimeError("Install manifest is missing")
        if self.manifest_path.is_symlink() or self.server_path.is_symlink():
            raise RuntimeError("Install manifest and runtime must not be symbolic links")
        if self.manifest_path.stat().st_mode & 0o077:
            raise RuntimeError("Install manifest permissions are too broad")
        if self.api_key_file.is_symlink():
            raise RuntimeError("Local API credential must not be a symbolic link")
        manifest = json.loads(self.manifest_path.read_text())
        expected_identity = {
            "schemaVersion": 1,
            "provider": "qwen-local",
            "modelRevision": QWEN_REVISION,
            "modelSha256": self.model_sha256,
            "runtimeRevision": LLAMA_CPP_REVISION,
            "runtimeSha256": self.server_sha256,
            "modelPath": str(self.model_path),
            "serverPath": str(self.server_path),
            "apiKeyFile": str(self.api_key_file),
        }
        if any(manifest.get(key) != value for key, value in expected_identity.items()):
            raise RuntimeError("Install manifest identity does not match the managed installation")
        self._verify(self.model_path, manifest["modelSha256"], "installed Qwen model")
        self._verify(self.server_path, manifest["runtimeSha256"], "installed llama-server")
        key_stat = self.api_key_file.stat()
        if key_stat.st_mode & 0o077:
            raise RuntimeError("Local API credential permissions are too broad")
        key = self.api_key_file.read_text().strip()
        if len(key) < 32 or any(character.isspace() for character in key):
            raise RuntimeError("Local API credential is invalid")
        return manifest

    def uninstall(self) -> dict:
        """Remove only a verified installation with no unknown files."""
        managed_directories = {Path("models"), Path("bin"), Path("run")}
        if any((self.target_dir / relative).is_symlink() for relative in managed_directories):
            raise RuntimeError("Refusing to uninstall through a symbolic-link directory")
        self.verify_installation()
        allowed_files = {
            Path("models/Qwen3-Embedding-4B-Q5_K_M.gguf"),
            Path("bin/llama-server"),
            Path("run/api-key"),
            Path("run/llama-server.stdout.log"),
            Path("run/llama-server.stderr.log"),
            Path("install-manifest.json"),
        }
        actual = {path.relative_to(self.target_dir) for path in self.target_dir.rglob("*")}
        unexpected = sorted(str(path) for path in actual - allowed_files - managed_directories)
        if unexpected:
            raise RuntimeError(f"Refusing to uninstall a target with unexpected files: {unexpected[:5]}")

        removed = []
        for relative in sorted(allowed_files, key=lambda item: len(item.parts), reverse=True):
            path = self.target_dir / relative
            if path.exists() or path.is_symlink():
                path.unlink()
                removed.append(str(relative))
        for relative in sorted(managed_directories, key=lambda item: len(item.parts), reverse=True):
            path = self.target_dir / relative
            if path.exists():
                path.rmdir()
        self.target_dir.rmdir()
        return {"status": "uninstalled", "removedManagedFiles": len(removed)}
