from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from src.installer.qwen_installer import (
    QwenInstaller,
    atomic_json_write,
    sha256_file,
    validate_runtime_version_output,
)
from src.lifecycle.llama_server_manager import LlamaServerManager, _atomic_json_state, _open_safe_append


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact(tmp_path: Path, name: str, data: bytes, executable: bool = False) -> Path:
    file_path = tmp_path / name
    file_path.write_bytes(data)
    if executable:
        file_path.chmod(0o700)
    return file_path


@pytest.mark.parametrize("plant", ["symlink", "hardlink"])
def test_atomic_manifest_write_refuses_preplanted_staging_link(tmp_path: Path, plant: str) -> None:
    manifest = tmp_path / "install-manifest.json"
    outside = artifact(tmp_path, "outside-user-file", b"do-not-overwrite")
    temporary = manifest.with_suffix(".json.tmp")
    if plant == "symlink":
        temporary.symlink_to(outside)
    else:
        os.link(outside, temporary)

    with pytest.raises(RuntimeError, match="staging path"):
        atomic_json_write(manifest, {"provider": "qwen-local"})

    assert outside.read_bytes() == b"do-not-overwrite"
    assert not manifest.exists()


@pytest.mark.parametrize("plant", ["symlink", "hardlink"])
def test_pid_staging_write_refuses_preplanted_link(tmp_path: Path, plant: str) -> None:
    pid_file = tmp_path / "llama-server.pid.json"
    outside = artifact(tmp_path, "outside-pid-target", b"do-not-overwrite")
    temporary = pid_file.with_suffix(".json.tmp")
    if plant == "symlink":
        temporary.symlink_to(outside)
    else:
        os.link(outside, temporary)

    with pytest.raises(RuntimeError, match="PID staging"):
        _atomic_json_state(pid_file, {"pid": 123})

    assert outside.read_bytes() == b"do-not-overwrite"
    assert not pid_file.exists()


@pytest.mark.parametrize("plant", ["symlink", "hardlink"])
def test_log_append_refuses_preplanted_link(tmp_path: Path, plant: str) -> None:
    log = tmp_path / "llama-server.stdout.log"
    outside = artifact(tmp_path, "outside-log-target", b"do-not-append")
    if plant == "symlink":
        log.symlink_to(outside)
    else:
        os.link(outside, log)

    with pytest.raises(RuntimeError, match="log path"):
        _open_safe_append(log)

    assert outside.read_bytes() == b"do-not-append"


def test_runtime_version_gate_matches_official_build_and_platform() -> None:
    validate_runtime_version_output(
        "version: 0.3.0-dev (build 10625, commit 0cc5b1495)\n"
        "built with AppleClang 21.0.0.21000101 for Darwin arm64\n"
    )
    with pytest.raises(RuntimeError, match="build 10625"):
        validate_runtime_version_output("version: 0.3.0-dev (build 10624, commit attacker) for Darwin arm64")
    with pytest.raises(RuntimeError, match="Darwin arm64"):
        validate_runtime_version_output("version: 0.3.0-dev (build 10625, commit 0cc5b1495) for Linux x64")


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
    assert manifest["runtimePort"] == 18888
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


def test_installer_refuses_preplanted_api_key_hardlink_without_chmod(tmp_path: Path) -> None:
    target = tmp_path / "managed" / "qwen"
    installer = QwenInstaller(target)
    installer._ensure_dirs()
    outside = artifact(tmp_path, "outside-api-key", b"outside-user-value")
    outside.chmod(0o644)
    os.link(outside, installer.api_key_file)

    with pytest.raises(RuntimeError, match="link count"):
        installer._ensure_key()

    assert outside.read_bytes() == b"outside-user-value"
    assert outside.stat().st_mode & 0o777 == 0o644


def test_installer_refuses_symlinked_runtime_without_mutating_source(tmp_path: Path) -> None:
    model = artifact(tmp_path, "model.gguf", b"model")
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    target = tmp_path / "managed" / "qwen"
    installer = QwenInstaller(
        target,
        model_sha256=digest(model.read_bytes()),
        server_sha256=digest(server.read_bytes()),
    )
    installer.server_path.parent.mkdir(parents=True)
    installer.server_path.symlink_to(server)
    mode_before = server.stat().st_mode

    with pytest.raises(RuntimeError, match="must not be a symbolic link"):
        installer.install_from_verified_sources(model_source=model, server_source=server)

    assert server.stat().st_mode == mode_before


@pytest.mark.parametrize("link_kind", ["models-directory", "model-file"])
def test_production_installer_refuses_preplanted_model_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_kind: str
) -> None:
    model = artifact(tmp_path, "source-model.gguf", b"verified-model")
    archive = artifact(tmp_path, "runtime.tar.gz", b"verified-runtime")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = artifact(outside, "do-not-overwrite.gguf", b"user-owned")
    target = tmp_path / "managed" / "qwen"
    target.mkdir(parents=True)
    if link_kind == "models-directory":
        (target / "models").symlink_to(outside, target_is_directory=True)
    else:
        (target / "models").mkdir()
        (target / "models" / "Qwen3-Embedding-4B-Q5_K_M.gguf").symlink_to(outside_file)
    installer = QwenInstaller(target)
    monkeypatch.setattr(installer, "_verify", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="symbolic link"):
        installer.install_from_artifacts(model_source=model, runtime_archive=archive)

    assert outside_file.read_bytes() == b"user-owned"


def test_production_installer_refuses_preplanted_model_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = artifact(tmp_path, "source-model.gguf", b"verified-model")
    archive = artifact(tmp_path, "runtime.tar.gz", b"verified-runtime")
    outside = artifact(tmp_path, "outside-user-model", b"do-not-overwrite")
    target = tmp_path / "managed" / "qwen"
    (target / "models").mkdir(parents=True)
    os.link(outside, target / "models" / "Qwen3-Embedding-4B-Q5_K_M.gguf")
    installer = QwenInstaller(target)
    monkeypatch.setattr(installer, "_verify", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="link count"):
        installer.install_from_artifacts(model_source=model, runtime_archive=archive)

    assert outside.read_bytes() == b"do-not-overwrite"


@pytest.mark.parametrize("plant", ["symlink", "hardlink"])
def test_production_installer_refuses_preplanted_model_staging_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: str
) -> None:
    model = artifact(tmp_path, "source-model.gguf", b"verified-model")
    archive = artifact(tmp_path, "runtime.tar.gz", b"verified-runtime")
    outside = artifact(tmp_path, "outside-model-stage", b"do-not-overwrite")
    target = tmp_path / "managed" / "qwen"
    (target / "models").mkdir(parents=True)
    temporary = target / "models" / "Qwen3-Embedding-4B-Q5_K_M.gguf.tmp"
    if plant == "symlink":
        temporary.symlink_to(outside)
    else:
        os.link(outside, temporary)
    installer = QwenInstaller(target)
    monkeypatch.setattr(installer, "_verify", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Model staging"):
        installer.install_from_artifacts(model_source=model, runtime_archive=archive)

    assert outside.read_bytes() == b"do-not-overwrite"


def test_uninstaller_removes_only_verified_managed_artifacts(tmp_path: Path) -> None:
    model = artifact(tmp_path, "model.gguf", b"model")
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    target = tmp_path / "managed" / "qwen"
    installer = QwenInstaller(
        target,
        model_sha256=digest(model.read_bytes()),
        server_sha256=digest(server.read_bytes()),
    )
    installer.install_from_verified_sources(model_source=model, server_source=server)
    (installer.target_dir / "run" / "llama-server.stdout.log").write_text("fixture log")

    result = installer.uninstall()

    assert result["status"] == "uninstalled"
    assert not target.exists()
    assert model.exists()
    assert server.exists()


def test_uninstaller_refuses_unknown_files_without_partial_deletion(tmp_path: Path) -> None:
    model = artifact(tmp_path, "model.gguf", b"model")
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    target = tmp_path / "managed" / "qwen"
    installer = QwenInstaller(
        target,
        model_sha256=digest(model.read_bytes()),
        server_sha256=digest(server.read_bytes()),
    )
    installer.install_from_verified_sources(model_source=model, server_source=server)
    unknown = target / "do-not-delete.txt"
    unknown.write_text("user data")

    with pytest.raises(RuntimeError, match="unexpected files"):
        installer.uninstall()

    assert unknown.read_text() == "user data"
    assert installer.verify_installation()["provider"] == "qwen-local"


def test_uninstaller_refuses_symlinked_managed_directory(tmp_path: Path) -> None:
    model = artifact(tmp_path, "model.gguf", b"model")
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    target = tmp_path / "managed" / "qwen"
    installer = QwenInstaller(
        target,
        model_sha256=digest(model.read_bytes()),
        server_sha256=digest(server.read_bytes()),
    )
    installer.install_from_verified_sources(model_source=model, server_source=server)
    outside = tmp_path / "outside"
    outside.mkdir()
    installer.api_key_file.unlink()
    (target / "run").rmdir()
    (target / "run").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symbolic-link directory"):
        installer.uninstall()

    assert outside.exists()


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


def test_lifecycle_stop_reaps_a_managed_child_without_waiting_for_kill_timeout(tmp_path: Path) -> None:
    model = artifact(tmp_path, "model.gguf", b"model")
    key_file = artifact(tmp_path, "api-key", b"local-secret-value")
    key_file.chmod(0o600)
    tail = shutil.which("tail")
    assert tail is not None
    server = tmp_path / "llama-server"
    shutil.copyfile(tail, server)
    server.chmod(0o700)
    manager = LlamaServerManager(
        server_binary=server,
        model_path=model,
        api_key_file=key_file,
        state_dir=tmp_path / "state",
    )
    manager.state_dir.mkdir(parents=True)
    manager.process = subprocess.Popen(
        [str(server), "-f", str(model.resolve())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    manager.pid_file.write_text(json.dumps({"schemaVersion": 1, "pid": manager.process.pid, "port": 18888}))
    time.sleep(0.1)

    started = time.monotonic()
    manager.stop(timeout_seconds=2)

    assert time.monotonic() - started < 2
    assert manager.process is None
    assert not manager.pid_file.exists()


def test_lifecycle_recovered_pid_still_requires_process_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    model = artifact(tmp_path, "model.gguf", b"model")
    key_file = artifact(tmp_path, "api-key", b"local-secret-value")
    key_file.chmod(0o600)
    manager = LlamaServerManager(
        server_binary=server,
        model_path=model,
        api_key_file=key_file,
        state_dir=tmp_path / "state",
    )
    manager.state_dir.mkdir(parents=True)
    manager.pid_file.write_text(json.dumps({
        "schemaVersion": 2,
        "pid": 4242,
        "port": 18888,
        "serverBinary": str(server.resolve()),
        "modelPath": str(model.resolve()),
    }))
    monkeypatch.setattr(manager, "_process_exists", lambda _pid: True)
    monkeypatch.setattr(manager, "_is_expected_process", lambda _pid: False)

    with pytest.raises(RuntimeError, match="not the managed llama-server"):
        manager.stop()


def test_uninstall_refuses_unknown_listener_without_pid_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = artifact(tmp_path, "llama-server", b"server", executable=True)
    model = artifact(tmp_path, "model.gguf", b"model")
    key_file = artifact(tmp_path, "api-key", b"local-secret-value")
    key_file.chmod(0o600)
    manager = LlamaServerManager(
        server_binary=server,
        model_path=model,
        api_key_file=key_file,
        state_dir=tmp_path / "state",
    )
    monkeypatch.setattr(manager, "_is_port_in_use", lambda: True)

    with pytest.raises(RuntimeError, match="without managed PID state"):
        manager.stop_for_uninstall()
