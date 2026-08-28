from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.installer.qwen_installer import QwenInstaller, sha256_file
from src.lifecycle.llama_server_manager import LlamaServerManager


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact(tmp_path: Path, name: str, data: bytes, executable: bool = False) -> Path:
    file_path = tmp_path / name
    file_path.write_bytes(data)
    if executable:
        file_path.chmod(0o700)
    return file_path


def test_installer_verifies_both_artifacts_and_writes_restricted_manifest(tmp_path: Path) -> None:
    model = artifact(tmp_path, "model.gguf", b"verified-model")
    server = artifact(tmp_path, "llama-server", b"verified-runtime", executable=True)
    installer = QwenInstaller(
        tmp_path / "managed" / "qwen",
        model_sha256=digest(model.read_bytes()),
        server_sha256=digest(server.read_bytes()),
    )
    manifest = installer.install_from_verified_sources(
        model_source=model,
        server_source=server,
        development_model_link=True,
    )
    assert installer.model_path.is_symlink()
    assert sha256_file(installer.model_path) == digest(model.read_bytes())
    assert sha256_file(installer.server_path) == digest(server.read_bytes())
    assert installer.api_key_file.stat().st_mode & 0o077 == 0
    assert installer.manifest_path.stat().st_mode & 0o077 == 0
    assert manifest["provider"] == "qwen-local"
    assert installer.verify_installation()["runtimeSha256"] == digest(server.read_bytes())


def test_installer_fails_closed_on_wrong_hash_or_broad_key_permissions(tmp_path: Path) -> None:
    model = artifact(tmp_path, "model.gguf", b"model")
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    installer = QwenInstaller(
        tmp_path / "managed" / "qwen",
        model_sha256="0" * 64,
        server_sha256=digest(server.read_bytes()),
    )
    with pytest.raises(RuntimeError, match="SHA-256"):
        installer.install_from_verified_sources(model_source=model, server_source=server)

    good = QwenInstaller(
        tmp_path / "managed" / "good",
        model_sha256=digest(model.read_bytes()),
        server_sha256=digest(server.read_bytes()),
    )
    good.install_from_verified_sources(model_source=model, server_source=server)
    good.api_key_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions"):
        good.verify_installation()


def test_installer_rejects_symlinked_api_key(tmp_path: Path) -> None:
    model = artifact(tmp_path, "model.gguf", b"model")
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    installer = QwenInstaller(
        tmp_path / "managed" / "qwen",
        model_sha256=digest(model.read_bytes()),
        server_sha256=digest(server.read_bytes()),
    )
    installer.install_from_verified_sources(model_source=model, server_source=server)
    real_key = artifact(tmp_path, "other-key", b"x" * 48)
    real_key.chmod(0o600)
    installer.api_key_file.unlink()
    installer.api_key_file.symlink_to(real_key)
    with pytest.raises(RuntimeError, match="symbolic link"):
        installer.verify_installation()


def test_lifecycle_command_is_loopback_embedding_only_and_uses_last_pooling(tmp_path: Path) -> None:
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    model = artifact(tmp_path, "model.gguf", b"model")
    key_file = artifact(tmp_path, "api-key", b"local-secret-value")
    key_file.chmod(0o600)
    manager = LlamaServerManager(
        server_binary=server,
        model_path=model,
        api_key_file=key_file,
        state_dir=tmp_path / "state",
        port=18888,
    )
    command = manager.command()
    assert command[0] == str(server.resolve())
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--pooling") + 1] == "last"
    assert "--embedding" in command
    assert "--no-webui" in command
    assert "--api-key-file" in command
    assert "none" not in command


def test_lifecycle_refuses_broad_credential_permissions(tmp_path: Path) -> None:
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    model = artifact(tmp_path, "model.gguf", b"model")
    key_file = artifact(tmp_path, "api-key", b"local-secret-value")
    key_file.chmod(0o644)
    manager = LlamaServerManager(
        server_binary=server,
        model_path=model,
        api_key_file=key_file,
        state_dir=tmp_path / "state",
    )
    with pytest.raises(PermissionError, match="permissions"):
        manager.command()
