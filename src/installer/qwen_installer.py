from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

from .artifacts import LLAMA_CPP, QWEN_MODEL
from .downloader import sha256_file
from .safe_archive import extract_verified_tar

SCHEMA_VERSION = 2
PROVIDER = "qwen-local"
DEFAULT_TARGET = Path.home() / "Library/Application Support/OpenClaw/qwen-local"
RUNTIME_VERSION_MARKER = "build 10625, commit 0cc5b1495"
RUNTIME_PLATFORM_MARKER = "Darwin arm64"


def validate_runtime_version_output(output: str) -> None:
    if RUNTIME_VERSION_MARKER not in output or RUNTIME_PLATFORM_MARKER not in output:
        raise RuntimeError("llama-server version does not match build 10625 for Darwin arm64")


def atomic_json_write(file_path: Path, payload: dict) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, file_path)


class QwenInstaller:
    def __init__(
        self,
        target_dir: str | Path = DEFAULT_TARGET,
        *,
        model_sha256: str = QWEN_MODEL.sha256,
        server_sha256: str | None = None,
    ) -> None:
        raw = Path(target_dir).expanduser()
        if raw == Path(raw.anchor) or len(raw.parts) < 4:
            raise ValueError("Installer target must be a specific managed directory")
        self.target_dir = raw.resolve(strict=False)
        home = Path.home().resolve()
        workspace = (home / ".openclaw/workspace").resolve(strict=False)
        forbidden = {home, workspace, home / ".openclaw", Path("/")}
        if self.target_dir in forbidden or self.target_dir.name in {"knowledge-lancedb", "openclaw-lancedb-knowledge-skill"}:
            raise ValueError("Installer target overlaps a protected root or Gemini product path")
        parent = self.target_dir.parent
        if parent.exists() and parent.is_symlink():
            raise ValueError("Installer target parent must not be a symbolic link")
        if parent.exists() and parent.stat().st_uid != os.getuid():
            raise ValueError("Installer target parent must be owned by the current user")
        self.model_sha256 = model_sha256
        self.server_sha256 = server_sha256
        self.model_path = self.target_dir / "models" / QWEN_MODEL.filename
        self.runtime_path = self.target_dir / "runtime"
        self.server_path = self.runtime_path / "llama-server"
        self.api_key_file = self.target_dir / "run" / "api-key"
        self.manifest_path = self.target_dir / "install-manifest.json"

    @staticmethod
    def system_preflight(*, minimum_ram_gib: int = 16, minimum_free_gib: int = 12) -> dict:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("Only macOS Apple Silicon is supported")
        ram_bytes = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
        free_bytes = shutil.disk_usage(Path.home()).free
        if ram_bytes < minimum_ram_gib * 1024**3:
            raise RuntimeError(f"At least {minimum_ram_gib} GiB RAM is required")
        if free_bytes < minimum_free_gib * 1024**3:
            raise RuntimeError(f"At least {minimum_free_gib} GiB free disk is required")
        return {"platform": "darwin-arm64", "ramBytes": ram_bytes, "freeBytes": free_bytes}

    @staticmethod
    def _verify(path: Path, digest: str, label: str) -> None:
        if not path.is_file() or sha256_file(path) != digest:
            raise RuntimeError(f"{label} SHA-256 mismatch")

    def _ensure_dirs(self) -> None:
        if self.target_dir.exists() and self.target_dir.is_symlink():
            raise RuntimeError("Managed target must not be a symbolic link")
        for directory in (self.target_dir, self.model_path.parent, self.api_key_file.parent):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    def _ensure_key(self) -> None:
        if self.api_key_file.is_symlink():
            raise RuntimeError("Local API credential must not be a symbolic link")
        if not self.api_key_file.exists():
            descriptor = os.open(self.api_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(secrets.token_urlsafe(48) + "\n")
        os.chmod(self.api_key_file, 0o600)

    def install_from_artifacts(self, *, model_source: str | Path, runtime_archive: str | Path) -> dict:
        model = Path(model_source).resolve()
        archive = Path(runtime_archive).resolve()
        self._verify(model, self.model_sha256, "Qwen model")
        self._verify(archive, LLAMA_CPP.sha256, "llama.cpp archive")
        if self.manifest_path.exists():
            return self.verify_installation()
        self._ensure_dirs()
        shutil.copy2(model, self.model_path)
        os.chmod(self.model_path, 0o600)
        candidate = self.target_dir / ".runtime-candidate"
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_dir():
                raise RuntimeError("Stale runtime candidate is unsafe")
            shutil.rmtree(candidate)
        if self.runtime_path.exists() or self.runtime_path.is_symlink():
            raise RuntimeError("Unverified runtime path already exists; quarantine it before retrying")
        try:
            inventory = extract_verified_tar(archive, candidate)
            candidate_server = candidate / "llama-server"
            if not candidate_server.is_file() or candidate_server.is_symlink():
                raise RuntimeError("Runtime archive must contain one safe top-level llama-server")
            os.chmod(candidate_server, 0o700)
            version = subprocess.run(
                [str(candidate_server), "--version"], shell=False, check=True,
                capture_output=True, text=True, timeout=60,
            )
            validate_runtime_version_output(version.stdout + version.stderr)
            os.replace(candidate, self.runtime_path)
        except Exception:
            if candidate.exists() and candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            raise
        self._ensure_key()
        manifest = self._manifest(inventory)
        atomic_json_write(self.manifest_path, manifest)
        return manifest

    def install_from_verified_sources(
        self, *, model_source: str | Path, server_source: str | Path, development_model_link: bool = False
    ) -> dict:
        """Fixture-only compatibility helper; production install uses install_from_artifacts."""
        model = Path(model_source).resolve()
        server = Path(server_source).resolve()
        self._verify(model, self.model_sha256, "Qwen model")
        if self.server_sha256:
            self._verify(server, self.server_sha256, "llama-server")
        self._ensure_dirs()
        if not self.model_path.exists():
            if development_model_link:
                self.model_path.symlink_to(model)
            else:
                shutil.copy2(model, self.model_path)
        self.server_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.server_path.is_symlink():
            raise RuntimeError("Managed llama-server path must not be a symbolic link")
        if not self.server_path.exists():
            shutil.copy2(server, self.server_path)
        os.chmod(self.server_path, 0o700)
        self._ensure_key()
        inventory = [{"path": "llama-server", "bytes": self.server_path.stat().st_size,
                      "sha256": sha256_file(self.server_path), "executable": True}]
        manifest = self._manifest(inventory, fixture=True, development_model_link=development_model_link)
        atomic_json_write(self.manifest_path, manifest)
        return manifest

    def _manifest(self, inventory: list[dict], *, fixture: bool = False, development_model_link: bool = False) -> dict:
        manifest = {
            "schemaVersion": SCHEMA_VERSION,
            "provider": PROVIDER,
            "platform": "darwin-arm64" if not fixture else "test-fixture",
            "installRoot": str(self.target_dir),
            "model": QWEN_MODEL.filename.removesuffix(".gguf"),
            "modelRevision": QWEN_MODEL.revision,
            "modelSha256": self.model_sha256,
            "runtimeRelease": LLAMA_CPP.revision,
            "runtimeCommit": "0cc5b14959ee3a813bd787baaef50a170493547a",
            "runtimeArchiveSha256": LLAMA_CPP.sha256 if not fixture else "test-fixture",
            "runtimeInventory": inventory,
            "modelPath": str(self.model_path),
            "serverPath": str(self.server_path),
            "apiKeyFile": str(self.api_key_file),
            "developmentModelLink": development_model_link,
        }
        if fixture:
            manifest["runtimeSha256"] = sha256_file(self.server_path)
        return manifest

    def verify_installation(self) -> dict:
        if not self.manifest_path.is_file() or self.manifest_path.is_symlink():
            raise RuntimeError("Install manifest is missing or unsafe")
        if self.manifest_path.stat().st_mode & 0o077:
            raise RuntimeError("Install manifest permissions are too broad")
        manifest = json.loads(self.manifest_path.read_text())
        identity = {"schemaVersion": SCHEMA_VERSION, "provider": PROVIDER, "installRoot": str(self.target_dir),
                    "modelPath": str(self.model_path), "serverPath": str(self.server_path),
                    "apiKeyFile": str(self.api_key_file), "modelRevision": QWEN_MODEL.revision,
                    "modelSha256": self.model_sha256, "runtimeRelease": LLAMA_CPP.revision,
                    "runtimeCommit": "0cc5b14959ee3a813bd787baaef50a170493547a"}
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise RuntimeError("Install manifest identity does not match the managed installation")
        self._verify(self.model_path, self.model_sha256, "installed Qwen model")
        if self.server_path.is_symlink() or not self.server_path.is_file():
            raise RuntimeError("Installed llama-server is missing or unsafe")
        if self.server_sha256:
            self._verify(self.server_path, self.server_sha256, "installed llama-server")
        inventory = manifest.get("runtimeInventory")
        if not isinstance(inventory, list) or not inventory:
            raise RuntimeError("Runtime inventory is missing")
        for item in inventory:
            relative = Path(str(item.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise RuntimeError("Runtime inventory contains an unsafe path")
            installed = self.runtime_path / relative
            if installed.is_symlink() or not installed.is_file():
                raise RuntimeError("Runtime inventory file is missing or unsafe")
            if installed.stat().st_size != item.get("bytes") or sha256_file(installed) != item.get("sha256"):
                raise RuntimeError("Runtime inventory integrity mismatch")
        if self.api_key_file.is_symlink():
            raise RuntimeError("Local API credential must not be a symbolic link")
        if not self.api_key_file.is_file():
            raise RuntimeError("Local API credential is missing")
        if self.api_key_file.stat().st_mode & 0o077:
            raise RuntimeError("Local API credential permissions are too broad")
        key = self.api_key_file.read_text().strip()
        if len(key) < 32 or any(character.isspace() for character in key):
            raise RuntimeError("Local API credential is invalid")
        return manifest

    def uninstall(self) -> dict:
        managed_dirs = (self.target_dir / "models", self.target_dir / "runtime", self.target_dir / "run")
        if any(path.is_symlink() for path in managed_dirs):
            raise RuntimeError("Refusing to uninstall through a symbolic-link directory")
        self.verify_installation()
        allowed_dirs = {Path("models"), Path("runtime"), Path("runtime/bin"), Path("run")}
        allowed_files = {Path("models") / QWEN_MODEL.filename, Path("run/api-key"),
                         Path("run/llama-server.pid.json"), Path("run/llama-server.stdout.log"),
                         Path("run/llama-server.stderr.log"), Path("install-manifest.json")}
        manifest = json.loads(self.manifest_path.read_text())
        allowed_files.update(Path("runtime") / item["path"] for item in manifest.get("runtimeInventory", []))
        for file_path in tuple(allowed_files):
            allowed_dirs.update(file_path.parents)
        allowed_dirs.discard(Path("."))
        allowed_files.add(Path("runtime/llama-server"))
        actual = {path.relative_to(self.target_dir) for path in self.target_dir.rglob("*")}
        for path in actual:
            if (self.target_dir / path).is_symlink() and path != Path("models") / QWEN_MODEL.filename:
                raise RuntimeError("Refusing to uninstall a target containing a symbolic link")
        unexpected = sorted(str(path) for path in actual - allowed_files - allowed_dirs)
        if unexpected:
            raise RuntimeError(f"Refusing to uninstall a target with unexpected files: {unexpected[:5]}")
        shutil.rmtree(self.target_dir)
        return {"status": "uninstalled", "removedManagedRoot": True}
