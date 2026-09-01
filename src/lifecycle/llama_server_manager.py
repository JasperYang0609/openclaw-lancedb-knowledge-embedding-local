from __future__ import annotations

import json
import math
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


class LlamaServerManager:
    def __init__(
        self,
        *,
        server_binary: str | Path,
        model_path: str | Path,
        api_key_file: str | Path,
        state_dir: str | Path,
        port: int = 18888,
    ) -> None:
        self.server_binary = Path(server_binary).expanduser().resolve()
        self.model_path = Path(model_path).expanduser().resolve()
        self.api_key_file = Path(api_key_file).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.port = int(port)
        if not 1024 <= self.port <= 65535:
            raise ValueError("Sidecar port must be from 1024 through 65535")
        self.pid_file = self.state_dir / "llama-server.pid.json"
        self.stdout_log = self.state_dir / "llama-server.stdout.log"
        self.stderr_log = self.state_dir / "llama-server.stderr.log"
        self.process: subprocess.Popen | None = None

    def validate_files(self) -> None:
        if not self.server_binary.is_file():
            raise FileNotFoundError("llama-server binary is missing")
        if not self.model_path.is_file():
            raise FileNotFoundError("Qwen model is missing")
        if not self.api_key_file.is_file():
            raise FileNotFoundError("Local API credential is missing")
        if self.api_key_file.stat().st_mode & 0o077:
            raise PermissionError("Local API credential permissions are too broad")
        if self.api_key_file.is_symlink():
            raise PermissionError("Local API credential must not be a symbolic link")

    def command(self) -> list[str]:
        self.validate_files()
        return [
            str(self.server_binary),
            "--model", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--embedding",
            "--pooling", "last",
            "--ctx-size", "4096",
            "--batch-size", "4096",
            "--ubatch-size", "2048",
            "--parallel", "1",
            "--no-webui",
            "--api-key-file", str(self.api_key_file),
        ]

    def _is_port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
            handle.settimeout(0.25)
            return handle.connect_ex(("127.0.0.1", self.port)) == 0

    def _api_key(self) -> str:
        return self.api_key_file.read_text().strip()

    def is_healthy(self) -> bool:
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}/health", method="GET")
            with urllib.request.urlopen(request, timeout=1) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def embedding_canary(self) -> bool:
        try:
            payload = json.dumps({
                "input": ["local embedding health canary"],
                "encoding_format": "float",
            }).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/embeddings",
                data=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key()}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                if response.status != 200:
                    return False
                data = json.loads(response.read().decode("utf-8")).get("data") or []
            vector = data[0].get("embedding") if len(data) == 1 else None
            return isinstance(vector, list) and len(vector) == 2560 and all(
                isinstance(value, (int, float)) and math.isfinite(value) for value in vector
            ) and math.sqrt(sum(float(value) ** 2 for value in vector)) > 0
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError,
                UnicodeDecodeError, ValueError, TypeError, IndexError):
            return False

    def stop_for_uninstall(self) -> None:
        """Stop a managed sidecar or refuse deletion when ownership is unknown."""
        if self.pid_file.exists() or (self.process and self.process.poll() is None):
            self.stop()
            return
        if self._is_port_in_use():
            raise RuntimeError(
                f"Refusing to uninstall while loopback port {self.port} is in use without managed PID state"
            )

    def start(self, *, timeout_seconds: int = 180) -> int:
        self.validate_files()
        if self.pid_file.is_file():
            try:
                metadata = json.loads(self.pid_file.read_text())
                recorded_pid = int(metadata["pid"])
            except (ValueError, KeyError, json.JSONDecodeError):
                raise RuntimeError("Invalid llama-server pid file")
            if self._process_exists(recorded_pid):
                if not self._pid_identity_matches(metadata) or not self._is_expected_process(recorded_pid):
                    raise RuntimeError("Recorded PID does not match the managed llama-server identity")
                if self.is_healthy() and self.embedding_canary():
                    return recorded_pid
                raise RuntimeError("Managed llama-server is running but unhealthy")
            self.pid_file.unlink()
        if self._is_port_in_use():
            raise RuntimeError(f"Loopback port {self.port} is already in use")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        stdout_handle = self.stdout_log.open("ab")
        stderr_handle = self.stderr_log.open("ab")
        self.process = subprocess.Popen(
            self.command(),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            shell=False,
            start_new_session=True,
        )
        started = time.time()
        while time.time() - started < timeout_seconds:
            if self.process.poll() is not None:
                raise RuntimeError(f"llama-server exited during startup with code {self.process.returncode}")
            if self.is_healthy() and self.embedding_canary():
                temporary = self.pid_file.with_suffix(".tmp")
                temporary.write_text(json.dumps({
                    "schemaVersion": 2,
                    "pid": self.process.pid,
                    "port": self.port,
                    "startedAtEpoch": time.time(),
                    "serverBinary": str(self.server_binary),
                    "modelPath": str(self.model_path),
                }, indent=2) + "\n")
                os.chmod(temporary, 0o600)
                temporary.replace(self.pid_file)
                return self.process.pid
            time.sleep(1)
        self.stop()
        raise RuntimeError("llama-server failed health and embedding canary before timeout")

    def stop(self, *, timeout_seconds: int = 30) -> None:
        managed_process = self.process if self.process and self.process.poll() is None else None
        pid = managed_process.pid if managed_process else None
        if pid is None and self.pid_file.is_file():
            try:
                metadata = json.loads(self.pid_file.read_text())
                pid = int(metadata["pid"])
            except (ValueError, KeyError, json.JSONDecodeError):
                raise RuntimeError("Invalid llama-server pid file")
            if not self._pid_identity_matches(metadata):
                raise RuntimeError("Refusing to stop a process with mismatched PID metadata")
        if pid is not None:
            if not self._process_exists(pid):
                pid = None
            # A live Popen handle is the exact child this manager spawned. PID
            # recovery after a restart has no such handle and must still pass
            # the executable/model command-line identity check before signal.
            elif managed_process is None and not self._is_expected_process(pid):
                raise RuntimeError("Refusing to signal a PID that is not the managed llama-server")
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if managed_process is not None:
                try:
                    managed_process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    managed_process.kill()
                    managed_process.wait(timeout=5)
            else:
                deadline = time.time() + timeout_seconds
                while time.time() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.25)
                else:
                    os.kill(pid, signal.SIGKILL)
        self.process = None
        self.pid_file.unlink(missing_ok=True)

    def status(self) -> dict:
        metadata = None
        if self.pid_file.is_file():
            try:
                metadata = json.loads(self.pid_file.read_text())
            except (OSError, json.JSONDecodeError):
                metadata = None
        pid = int(metadata["pid"]) if metadata and isinstance(metadata.get("pid"), int) else None
        running = bool(pid and self._process_exists(pid) and self._pid_identity_matches(metadata)
                       and self._is_expected_process(pid))
        return {
            "running": running,
            "healthy": bool(running and self.is_healthy()),
            "pid": pid if running else None,
            "port": self.port,
            "endpoint": f"http://127.0.0.1:{self.port}",
        }

    def _pid_identity_matches(self, metadata: dict) -> bool:
        if metadata.get("schemaVersion") == 1:
            return metadata.get("port") == self.port
        return (
            metadata.get("schemaVersion") == 2
            and metadata.get("port") == self.port
            and metadata.get("serverBinary") == str(self.server_binary)
            and metadata.get("modelPath") == str(self.model_path)
        )

    @staticmethod
    def _process_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def _is_expected_process(self, pid: int) -> bool:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        command = result.stdout.strip()
        return str(self.server_binary) in command and str(self.model_path) in command
