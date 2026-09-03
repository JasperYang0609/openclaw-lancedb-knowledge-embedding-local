from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import uuid
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator

from .launchd import LAUNCHD_LABEL, build_launchd_plist

PLUGIN_ID = "openclaw-lancedb-knowledge-local"
SKILL_ID = "openclaw-lancedb-knowledge-local"
TOOL_NAME = "local_knowledge_search"
SNAPSHOT_MARKER_NAME = ".snapshot-run-id"
CRON_DECLARATION_KEY = "openclaw-lancedb-knowledge-local-incremental-v1"
GEMINI_DECLARATION_KEY = "openclaw-lancedb-knowledge-gemini-incremental-v1"
SCHEMA_VERSION = 1
FORBIDDEN_MANIFEST_KEYS = {"token", "secret", "password", "credential", "apiKey", "api_key", "query", "corpus", "vector"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_no_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        if component.exists() and component.is_symlink():
            raise ValueError("Managed integration paths must not contain symbolic links")


def _assert_specific_child(path: Path, parent: Path, label: str) -> None:
    resolved = path.resolve(strict=False)
    base = parent.resolve(strict=False)
    if resolved == base or base not in resolved.parents:
        raise ValueError(f"{label} must be a specific child of its managed root")


@dataclass(frozen=True)
class IntegrationPaths:
    home: Path
    workspace: Path
    project_root: Path
    runtime_root: Path
    state_root: Path
    launchd_plist: Path

    @classmethod
    def defaults(cls) -> "IntegrationPaths":
        home = Path.home().resolve()
        workspace = home / ".openclaw/workspace"
        return cls(
            home=home,
            workspace=workspace,
            project_root=workspace / "knowledge-lancedb-qwen-local",
            runtime_root=home / "Library/Application Support/OpenClaw/qwen-local",
            state_root=home / "Library/Application Support/OpenClaw/qwen-local-integration",
            launchd_plist=home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
        )

    def validate(self) -> None:
        values = (self.home, self.workspace, self.project_root, self.runtime_root, self.state_root, self.launchd_plist)
        if any(not Path(value).is_absolute() for value in values):
            raise ValueError("Integration paths must be absolute")
        if self.project_root.name != "knowledge-lancedb-qwen-local":
            raise ValueError("Qwen project root must use the managed Qwen project identity")
        if self.runtime_root.name != "qwen-local" or self.state_root.name != "qwen-local-integration":
            raise ValueError("Runtime and integration roots must use managed identities")
        if self.launchd_plist.name != f"{LAUNCHD_LABEL}.plist":
            raise ValueError("launchd plist identity does not match")
        for value in values:
            _assert_no_symlink_components(value)
        _assert_specific_child(self.workspace, self.home, "workspace")
        _assert_specific_child(self.project_root, self.workspace, "project root")
        _assert_specific_child(self.runtime_root, self.home, "runtime root")
        _assert_specific_child(self.state_root, self.home, "state root")


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key) in FORBIDDEN_MANIFEST_KEYS or _contains_forbidden_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


class TransactionStore:
    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(os.path.abspath(state_root))
        self.manifest_path = self.state_root / "transaction.json"

    def write(self, payload: dict[str, Any]) -> Path:
        if _contains_forbidden_key(payload):
            raise ValueError("Transaction manifest contains a forbidden sensitive field")
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)
        if self.manifest_path.is_symlink():
            raise RuntimeError("Transaction manifest must not be a symbolic link")
        temporary = self.manifest_path.with_suffix(".json.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as error:
            raise RuntimeError("Transaction staging path is unsafe") from error
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.manifest_path)
            os.chmod(self.manifest_path, 0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return self.manifest_path

    def read(self) -> dict[str, Any]:
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise RuntimeError("Transaction manifest is missing or unsafe")
        metadata = self.manifest_path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.getuid():
            raise RuntimeError("Transaction manifest ownership is unsafe")
        if metadata.st_mode & 0o077:
            raise RuntimeError("Transaction manifest permissions are too broad")
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if payload.get("schemaVersion") != SCHEMA_VERSION:
            raise RuntimeError("Unsupported transaction manifest schema")
        return payload


def merge_allowlist(existing: Any, value: str, *, create_if_missing: bool = False) -> list[str] | None:
    if existing is None:
        return [value] if create_if_missing else None
    if not isinstance(existing, list) or any(not isinstance(item, str) for item in existing):
        raise RuntimeError("OpenClaw allowlist has an unexpected schema")
    return list(dict.fromkeys([*existing, value]))


def build_cron_add_args(*, project_root: Path, incremental_script: Path,
                        schedule: str = "17 3 * * *", timezone: str = "Asia/Taipei") -> list[str]:
    project = Path(project_root).resolve(strict=False)
    script = Path(incremental_script).resolve(strict=False)
    if project not in script.parents or script.name != "knowledge_index_incremental.sh":
        raise ValueError("Incremental script must be the managed project wrapper")
    return [
        "cron", "add", "--name", "Qwen local knowledge incremental index", "--cron", schedule,
        "--tz", timezone, "--command-argv", json.dumps([str(script)], separators=(",", ":")),
        "--command-cwd", str(project), "--timeout-seconds", "7200",
        "--no-output-timeout-seconds", "900", "--output-max-bytes", "65536",
        "--declaration-key", CRON_DECLARATION_KEY, "--no-deliver", "--json",
    ]


def _job_argv(job: dict[str, Any]) -> list[str]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
    argv = command.get("argv")
    return argv if isinstance(argv, list) and all(isinstance(item, str) for item in argv) else []


def owned_gemini_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    owned = []
    for job in jobs:
        argv = _job_argv(job)
        if job.get("declarationKey") == GEMINI_DECLARATION_KEY and len(argv) == 1 and \
                Path(argv[0]).name == "knowledge_index_incremental.sh" and "knowledge-lancedb" in argv[0]:
            owned.append(job)
    return owned


class OpenClawCli:
    def __init__(self, executable: str | Path, *, profile: str | None = None,
                 runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self.executable = str(Path(executable).resolve())
        self.profile = profile
        self.runner = runner

    def command(self, args: list[str]) -> list[str]:
        return [self.executable, *(["--profile", self.profile] if self.profile else []), *args]

    def run(self, args: list[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.runner(self.command(args), shell=False, check=check, text=True, capture_output=True, timeout=timeout)

    def json(self, args: list[str], *, timeout: int = 120) -> Any:
        result = self.run(args, timeout=timeout)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("OpenClaw CLI returned invalid JSON") from error

    def config_get(self, path_name: str) -> Any:
        result = self.run(["config", "get", path_name, "--json"], check=False)
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("OpenClaw config get returned invalid JSON") from error


class IntegrationManager:
    def __init__(self, *, paths: IntegrationPaths, repo_root: Path, cli: OpenClawCli,
                 node_path: Path, agent: str = "main", launchctl: str | Path = "/bin/launchctl") -> None:
        self.paths = paths
        self.repo_root = Path(repo_root).resolve()
        self.cli = cli
        self.node_path = Path(node_path).resolve()
        self.agent = agent
        self.launchctl = str(launchctl)
        self.store = TransactionStore(paths.state_root)
        self.plugin_source = self.repo_root / "plugin" / PLUGIN_ID
        self.skill_source = self.repo_root / "openclaw-lancedb-knowledge-local"

    def preflight(self) -> dict[str, Any]:
        self.paths.validate()
        for file_path in (self.node_path, Path(self.cli.executable)):
            if file_path.is_symlink() or not file_path.is_file() or not os.access(file_path, os.X_OK):
                raise RuntimeError("Required executable is missing or unsafe")
        for directory in (self.plugin_source, self.skill_source):
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("Integration source package is missing or unsafe")
        runtime_manifest = self.paths.runtime_root / "install-manifest.json"
        if runtime_manifest.is_symlink() or not runtime_manifest.is_file():
            raise RuntimeError("Qwen runtime must be installed and verified before OpenClaw integration")
        if self.paths.launchd_plist.exists():
            try:
                existing_plist = plistlib.loads(self.paths.launchd_plist.read_bytes())
            except Exception as error:
                raise RuntimeError("Existing launchd plist is not a valid managed plist") from error
            argv = existing_plist.get("ProgramArguments", [])
            expected_server = str(self.paths.runtime_root / "runtime/llama-server")
            if existing_plist.get("Label") != LAUNCHD_LABEL or not isinstance(argv, list) or not argv or argv[0] != expected_server:
                raise RuntimeError("Existing launchd label is not owned by this Qwen installation")
        version = self.cli.run(["--version"]).stdout.strip()
        if "2026.7.1-2" not in version:
            raise RuntimeError("OpenClaw version has not passed this integration compatibility gate")
        self.cli.run(["config", "validate", "--json"])
        return {"openclawCompatible": True, "pluginSource": True, "skillSource": True}

    @staticmethod
    def _validate_private_directory(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError("OpenClaw config parent ownership is unsafe")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("OpenClaw config parent permissions are too broad")

    @staticmethod
    def _validate_restricted_directory(metadata: os.stat_result) -> None:
        IntegrationManager._validate_private_directory(metadata)
        if metadata.st_mode & 0o077:
            raise RuntimeError("OpenClaw integration state permissions are too broad")

    @staticmethod
    def _validate_private_config(metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.getuid():
            raise RuntimeError("OpenClaw config file ownership is unsafe")
        if metadata.st_mode & 0o077:
            raise RuntimeError("OpenClaw config file permissions are too broad")

    @contextmanager
    def _open_private_directory(self, directory: Path, *, create: bool = False) -> Iterator[int]:
        absolute = Path(os.path.abspath(directory))
        home = Path(os.path.abspath(self.paths.home))
        if absolute != home and home not in absolute.parents:
            raise ValueError("Managed directory must remain inside the OpenClaw home")
        relative = absolute.relative_to(home)
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise RuntimeError("Secure OpenClaw config traversal is unsupported on this platform")
        nofollow = os.O_NOFOLLOW
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
        directory_fds: list[int] = []
        try:
            try:
                current_fd = os.open(home, directory_flags)
                directory_fds.append(current_fd)
                self._validate_private_directory(os.fstat(current_fd))
                for component in relative.parts:
                    if create:
                        try:
                            os.mkdir(component, mode=0o700, dir_fd=current_fd)
                        except FileExistsError:
                            pass
                    current_fd = os.open(component, directory_flags, dir_fd=current_fd)
                    directory_fds.append(current_fd)
                    self._validate_private_directory(os.fstat(current_fd))
            except OSError as error:
                raise RuntimeError("Managed directory path is missing or unsafe") from error
            yield current_fd
        finally:
            for descriptor in reversed(directory_fds):
                os.close(descriptor)

    @contextmanager
    def _open_config_file(self, config_path: Path) -> Iterator[BinaryIO]:
        absolute = Path(os.path.abspath(config_path))
        home = Path(os.path.abspath(self.paths.home))
        if absolute == home or home not in absolute.parents:
            raise ValueError("OpenClaw config must be a specific child of its managed root")
        file_fd: int | None = None
        with self._open_private_directory(absolute.parent) as parent_fd:
            try:
                file_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW
                file_fd = os.open(absolute.name, file_flags, dir_fd=parent_fd)
                self._validate_private_config(os.fstat(file_fd))
            except OSError as error:
                raise RuntimeError("OpenClaw config path is missing or unsafe") from error
            try:
                with os.fdopen(file_fd, "rb", closefd=True) as handle:
                    file_fd = None
                    yield handle
            finally:
                if file_fd is not None:
                    os.close(file_fd)

    @staticmethod
    def _assert_stable_file(before: os.stat_result, after: os.stat_result) -> None:
        identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in identity):
            raise RuntimeError("OpenClaw config changed while it was being read")

    def _sha256_config(self, config_path: Path) -> str:
        digest = hashlib.sha256()
        with self._open_config_file(config_path) as handle:
            before = os.fstat(handle.fileno())
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
            self._assert_stable_file(before, after)
        return digest.hexdigest()

    def _config_file(self) -> Path:
        payload = self.cli.json(["config", "validate", "--json"])
        if not isinstance(payload, dict) or payload.get("valid") is not True \
                or not isinstance(payload.get("path"), str) or not payload["path"].strip():
            raise RuntimeError("OpenClaw config validation JSON has an unexpected schema")
        config_path = Path(payload["path"]).expanduser()
        if not config_path.is_absolute():
            raise RuntimeError("OpenClaw config validation JSON returned a relative path")
        _assert_no_symlink_components(config_path)
        _assert_specific_child(config_path, self.paths.home, "OpenClaw config")
        with self._open_config_file(config_path):
            pass
        return Path(os.path.abspath(config_path))

    @contextmanager
    def _integration_lock(self) -> Iterator[None]:
        lock_fd: int | None = None
        locked = False
        with self._open_private_directory(self.paths.state_root, create=True) as state_fd:
            try:
                lock_fd = os.open(
                    "integration.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600, dir_fd=state_fd,
                )
            except OSError as error:
                raise RuntimeError("OpenClaw integration lock is missing or unsafe") from error
            try:
                self._validate_private_config(os.fstat(lock_fd))
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError as error:
                    raise RuntimeError("Another OpenClaw integration transaction is active") from error
                yield
            finally:
                if lock_fd is not None:
                    try:
                        if locked:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)

    @staticmethod
    def _validate_snapshot_run_name(name: str) -> None:
        if not name.startswith("run-"):
            raise RuntimeError("Snapshot run identity is unsafe")
        try:
            parsed = uuid.UUID(name[4:])
        except ValueError as error:
            raise RuntimeError("Snapshot run identity is unsafe") from error
        if str(parsed) != name[4:]:
            raise RuntimeError("Snapshot run identity is unsafe")

    def _remove_tree_at(self, parent_fd: int, name: str, expected_identity: tuple[int, int]) -> None:
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            metadata = os.fstat(directory_fd)
            self._validate_private_directory(metadata)
            if (metadata.st_dev, metadata.st_ino) != expected_identity:
                raise RuntimeError("Snapshot run changed before cleanup")
            for child_name in os.listdir(directory_fd):
                child = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(child.st_mode):
                    self._remove_tree_at(directory_fd, child_name, (child.st_dev, child.st_ino))
                else:
                    os.unlink(child_name, dir_fd=directory_fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != expected_identity:
                raise RuntimeError("Snapshot run changed before cleanup")
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(directory_fd)

    def _verify_snapshot_marker(self, run_fd: int, expected_sha256: str) -> None:
        marker_fd: int | None = None
        try:
            marker_fd = os.open(
                SNAPSHOT_MARKER_NAME,
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                dir_fd=run_fd,
            )
            self._validate_private_config(os.fstat(marker_fd))
            with os.fdopen(marker_fd, "rb", closefd=True) as handle:
                marker_fd = None
                before = os.fstat(handle.fileno())
                value = handle.read(256)
                if handle.read(1):
                    raise RuntimeError("Snapshot run marker is oversized")
                after = os.fstat(handle.fileno())
                self._assert_stable_file(before, after)
            if hashlib.sha256(value).hexdigest() != expected_sha256:
                raise RuntimeError("Snapshot run marker mismatch")
        except OSError as error:
            raise RuntimeError("Snapshot run marker is missing or unsafe") from error
        finally:
            if marker_fd is not None:
                os.close(marker_fd)

    def _remove_snapshot_run(self, run_dir: Path, expected_identity: tuple[int, int],
                             expected_marker_sha256: str) -> None:
        run_dir = Path(os.path.abspath(run_dir))
        expected_parent = Path(os.path.abspath(self.paths.state_root / "snapshots"))
        if run_dir.parent != expected_parent:
            raise RuntimeError("Snapshot run cleanup identity is unsafe")
        self._validate_snapshot_run_name(run_dir.name)
        with self._open_private_directory(expected_parent) as snapshot_fd:
            run_fd = os.open(run_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=snapshot_fd)
            try:
                metadata = os.fstat(run_fd)
                if (metadata.st_dev, metadata.st_ino) != expected_identity:
                    raise RuntimeError("Snapshot run changed before cleanup")
                self._verify_snapshot_marker(run_fd, expected_marker_sha256)
            finally:
                os.close(run_fd)
            self._remove_tree_at(snapshot_fd, run_dir.name, expected_identity)

    def _snapshot_run_from_backup(self, backup: Path) -> Path:
        backup = Path(os.path.abspath(backup))
        snapshot_root = Path(os.path.abspath(self.paths.state_root / "snapshots"))
        if backup.name != "openclaw-config.preinstall" or backup.parent.parent != snapshot_root:
            raise RuntimeError("Snapshot config identity is unsafe")
        self._validate_snapshot_run_name(backup.parent.name)
        return backup.parent

    def _remove_recorded_snapshot_run(self, backup: Path, expected_identity: tuple[int, int],
                                      expected_marker_sha256: str) -> bool:
        run_dir = self._snapshot_run_from_backup(backup)
        try:
            self._remove_snapshot_run(run_dir, expected_identity, expected_marker_sha256)
        except FileNotFoundError:
            with self._open_private_directory(run_dir.parent) as snapshot_fd:
                try:
                    os.stat(run_dir.name, dir_fd=snapshot_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return False
            raise
        return True

    def _snapshot_other_assets(self, snapshot_dir: Path) -> dict[str, Any]:
        skill_target = self.paths.workspace / "skills" / SKILL_ID
        skill_backup = snapshot_dir / "skill.preinstall"
        plist_backup = snapshot_dir / "launchd.preinstall.plist"
        project_backup = snapshot_dir / "project-runtime.preinstall"
        managed_backups = (skill_backup, plist_backup, project_backup)
        if any(candidate.exists() or candidate.is_symlink() for candidate in managed_backups):
            raise RuntimeError("A non-config preinstall snapshot already exists")
        skill_existed = skill_target.exists()
        plist_existed = self.paths.launchd_plist.exists()
        project_existed = self.paths.project_root.exists()
        if skill_target.is_symlink():
            raise RuntimeError("Existing local knowledge skill is a symbolic link")
        if skill_existed:
            shutil.copytree(skill_target, skill_backup)
        if self.paths.launchd_plist.is_symlink():
            raise RuntimeError("Existing launchd plist is a symbolic link")
        if plist_existed:
            shutil.copy2(self.paths.launchd_plist, plist_backup)
            os.chmod(plist_backup, 0o600)
        if project_existed:
            if self.paths.project_root.is_symlink() or not self.paths.project_root.is_dir():
                raise RuntimeError("Existing Qwen project root is unsafe")
            project_backup.mkdir(mode=0o700)
            for relative in (Path("src"), Path("scripts"), Path("package.json"), Path("package-lock.json")):
                source = self.paths.project_root / relative
                if not source.exists():
                    continue
                if source.is_symlink():
                    raise RuntimeError("Existing Qwen project runtime contains a symbolic link")
                target = project_backup / relative
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        return {
            "skillTargetPath": str(skill_target), "skillBackupPath": str(skill_backup),
            "skillExisted": skill_existed, "plistBackupPath": str(plist_backup), "plistExisted": plist_existed,
            "projectExisted": project_existed, "projectBackupPath": str(project_backup),
        }

    def snapshot(self) -> dict[str, Any]:
        config = self._config_file()
        snapshot_root = self.paths.state_root / "snapshots"
        run_dir = snapshot_root / f"run-{uuid.uuid4()}"
        backup = run_dir / "openclaw-config.preinstall"
        temporary_name = backup.name + ".tmp"
        digest = hashlib.sha256()
        run_fd: int | None = None
        run_identity: tuple[int, int] | None = None
        run_marker_sha256: str | None = None
        try:
            with self._open_private_directory(snapshot_root, create=True) as snapshot_fd:
                os.fchmod(snapshot_fd, 0o700)
                self._validate_restricted_directory(os.fstat(snapshot_fd))
                os.mkdir(run_dir.name, mode=0o700, dir_fd=snapshot_fd)
                run_fd = os.open(
                    run_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=snapshot_fd,
                )
                run_metadata = os.fstat(run_fd)
                self._validate_restricted_directory(run_metadata)
                run_identity = (run_metadata.st_dev, run_metadata.st_ino)
                marker_value = uuid.uuid4().hex.encode("ascii")
                marker_fd = os.open(
                    SNAPSHOT_MARKER_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=run_fd,
                )
                with os.fdopen(marker_fd, "wb", closefd=True) as marker_handle:
                    marker_handle.write(marker_value)
                    marker_handle.flush()
                    os.fsync(marker_handle.fileno())
                run_marker_sha256 = hashlib.sha256(marker_value).hexdigest()
                descriptor: int | None = os.open(
                    temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=run_fd,
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as target:
                        descriptor = None
                        with self._open_config_file(config) as source:
                            before = os.fstat(source.fileno())
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                digest.update(chunk)
                                target.write(chunk)
                            after = os.fstat(source.fileno())
                            self._assert_stable_file(before, after)
                            target.flush()
                            os.fsync(target.fileno())
                    os.replace(
                        temporary_name, backup.name,
                        src_dir_fd=run_fd, dst_dir_fd=run_fd,
                    )
                    os.fsync(run_fd)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            other_assets = self._snapshot_other_assets(run_dir)
        except Exception:
            if run_identity is not None and run_marker_sha256 is not None:
                try:
                    self._remove_snapshot_run(run_dir, run_identity, run_marker_sha256)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if run_fd is not None:
                os.close(run_fd)
        return {
            "configPath": str(config), "configBackupPath": str(backup), "preConfigSha256": digest.hexdigest(),
            "snapshotRunDev": run_identity[0], "snapshotRunIno": run_identity[1],
            "snapshotRunMarkerSha256": run_marker_sha256,
            **other_assets,
        }

    def install_launchd_plist(self, runtime_manifest: dict[str, Any]) -> None:
        logs = self.paths.state_root / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = build_launchd_plist(
            server=Path(runtime_manifest["serverPath"]), model=Path(runtime_manifest["modelPath"]),
            api_key_file=Path(runtime_manifest["apiKeyFile"]), port=int(runtime_manifest["runtimePort"]),
            stdout_path=logs / "server.out.log", stderr_path=logs / "server.err.log",
        )
        self.paths.launchd_plist.parent.mkdir(parents=True, exist_ok=True)
        if self.paths.launchd_plist.is_symlink():
            raise RuntimeError("launchd plist target is unsafe")
        temporary = self.paths.launchd_plist.with_suffix(".plist.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.paths.launchd_plist)

    def configure_openclaw(self, allowed_projects: list[str]) -> None:
        plugin_archive = self.package_plugin_archive()
        try:
            self.cli.run(["plugins", "install", str(plugin_archive)], timeout=600)
        finally:
            staging = plugin_archive.parent
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging)
        self.cli.run(["plugins", "enable", PLUGIN_ID])
        plugin_config = {
            "projectRoot": str(self.paths.project_root), "nodePath": str(self.node_path),
            "allowedProjects": allowed_projects, "timeoutMs": 30000, "maxOutputBytes": 262144,
        }
        self.cli.run(["config", "set", f"plugins.entries.{PLUGIN_ID}.config",
                      json.dumps(plugin_config, separators=(",", ":")), "--strict-json"])
        plugin_allow = merge_allowlist(self.cli.config_get("plugins.allow"), PLUGIN_ID, create_if_missing=True)
        if plugin_allow is not None:
            self.cli.run(["config", "set", "plugins.allow", json.dumps(plugin_allow), "--strict-json", "--replace"])
        tool_allow = merge_allowlist(self.cli.config_get("tools.allow"), TOOL_NAME)
        if tool_allow is not None:
            self.cli.run(["config", "set", "tools.allow", json.dumps(tool_allow), "--strict-json", "--replace"])
        self.cli.run(["skills", "install", str(self.skill_source), "--as", SKILL_ID, "--force", "--agent", self.agent], timeout=300)
        self.cli.run(["config", "validate", "--json"])

    def package_plugin_archive(self) -> Path:
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("npm is required to package the OpenClaw plugin")
        staging = self.paths.state_root / "plugin-package"
        if staging.is_symlink():
            raise RuntimeError("Plugin package staging path is unsafe")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, mode=0o700)
        safe_env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "TMP", "TEMP", "NO_PROXY")
                    if os.environ.get(key)}
        safe_env["npm_config_ignore_scripts"] = "true"
        result = subprocess.run([
            str(Path(npm).resolve()), "pack", "--json", "--ignore-scripts",
            "--pack-destination", str(staging),
        ], cwd=self.plugin_source, env=safe_env, shell=False, check=True, text=True,
            capture_output=True, timeout=300)
        try:
            payload = json.loads(result.stdout)
            filename = payload[0]["filename"]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
            raise RuntimeError("npm pack returned an invalid plugin archive description") from error
        archive = staging / str(filename)
        if archive.resolve(strict=False).parent != staging.resolve() or archive.is_symlink() or not archive.is_file():
            raise RuntimeError("Plugin archive path is unsafe")
        return archive

    def create_incremental_cron(self) -> str:
        script = self.paths.project_root / "scripts/knowledge_index_incremental.sh"
        payload = self.cli.json(build_cron_add_args(project_root=self.paths.project_root, incremental_script=script))
        job_id = payload.get("id") or payload.get("job", {}).get("id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("OpenClaw cron add did not return a job id")
        self.cli.run(["cron", "edit", job_id, "--failure-alert", "--failure-alert-after", "1",
                      "--failure-alert-cooldown", "1h", "--failure-alert-channel", "last"])
        return job_id

    def disable_owned_gemini_jobs(self) -> list[dict[str, Any]]:
        payload = self.cli.json(["cron", "list", "--all", "--json"])
        jobs = payload.get("jobs", payload) if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            raise RuntimeError("OpenClaw cron list returned an unexpected schema")
        disabled = []
        for job in owned_gemini_jobs(jobs):
            if job.get("enabled", True):
                self.cli.run(["cron", "disable", str(job["id"])])
                disabled.append({"id": str(job["id"]), "wasEnabled": True})
        return disabled

    def begin(self) -> dict[str, Any]:
        if self.store.manifest_path.is_file() and not self.store.manifest_path.is_symlink():
            prior = self.store.read()
            if prior.get("phase") == "rolled_back":
                backup_path = prior.get("configBackupPath")
                if not isinstance(backup_path, str) or not backup_path:
                    raise RuntimeError("Rolled-back transaction is missing its snapshot identity")
                run_dev = prior.get("snapshotRunDev")
                run_ino = prior.get("snapshotRunIno")
                marker_sha256 = prior.get("snapshotRunMarkerSha256")
                if type(run_dev) is not int or type(run_ino) is not int \
                        or not isinstance(marker_sha256, str) or len(marker_sha256) != 64:
                    raise RuntimeError("Rolled-back transaction is missing its snapshot identity")
                self._remove_recorded_snapshot_run(
                    Path(backup_path), (run_dev, run_ino), marker_sha256,
                )
        self.preflight()
        snapshot = self.snapshot()
        payload = {
            "schemaVersion": SCHEMA_VERSION, "runId": str(uuid.uuid4()), "phase": "prepared",
            "ownedAssets": [], **snapshot,
        }
        self.store.write(payload)
        return payload

    def _launchctl(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.launchctl, *args], shell=False, check=check, text=True,
                              capture_output=True, timeout=120)

    def activate_launchd(self) -> None:
        domain = f"gui/{os.getuid()}"
        self._launchctl(["bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
        self._launchctl(["bootstrap", domain, str(self.paths.launchd_plist)])
        self._launchctl(["kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}"])

    def deactivate_launchd(self) -> None:
        self._launchctl(["bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)

    def bootstrap_project(self, runtime_manifest: dict[str, Any]) -> bool:
        config = self.paths.project_root / "config/source-map.json"
        if config.is_file() and not config.is_symlink():
            return False
        if self.paths.project_root.exists() and any(self.paths.project_root.iterdir()):
            raise RuntimeError("Qwen project exists without a safe source-map config")
        bootstrap = self.skill_source / "scripts/bootstrap_openclaw_lancedb.py"
        endpoint = f"http://127.0.0.1:{int(runtime_manifest['runtimePort'])}"
        subprocess.run([
            sys.executable, str(bootstrap), "--target", str(self.paths.project_root),
            "--workspace", str(self.paths.workspace), "--api-key-file", str(runtime_manifest["apiKeyFile"]),
            "--endpoint", endpoint, "--npm-install",
        ], shell=False, check=True, text=True, capture_output=True, timeout=1800)
        return True

    def synchronize_project_runtime(self) -> None:
        template = self.skill_source / "assets/knowledge-lancedb-template"
        if template.is_symlink() or not template.is_dir():
            raise RuntimeError("Bundled Qwen project template is missing or unsafe")
        for relative in (Path("src"), Path("scripts"), Path("package.json"), Path("package-lock.json")):
            source = template / relative
            target = self.paths.project_root / relative
            if source.is_symlink() or not source.exists() or target.is_symlink():
                raise RuntimeError("Qwen project runtime synchronization boundary is unsafe")
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("npm executable is required to synchronize the Qwen project runtime")
        safe_env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "TMP", "TEMP", "NO_PROXY")
                    if os.environ.get(key)}
        safe_env["npm_config_ignore_scripts"] = "true"
        subprocess.run([str(Path(npm).resolve()), "ci", "--ignore-scripts"], cwd=self.paths.project_root,
                       env=safe_env, shell=False, check=True, text=True, capture_output=True, timeout=1800)

    def _allowed_projects(self) -> list[str]:
        config_path = self.paths.project_root / "config/source-map.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        projects = sorted({str(item.get("project", "")).strip() for item in config.get("sources", [])
                           if str(item.get("project", "")).strip()})
        if len(projects) > 100:
            raise RuntimeError("Project allowlist exceeds the supported limit")
        return projects

    def mark_ready_or_schedule_build(self) -> tuple[str, str | None]:
        cli_path = self.paths.project_root / "src/cli.js"
        audit = subprocess.run([str(self.node_path), str(cli_path), "audit", "--mark-ready"],
                               cwd=self.paths.project_root, shell=False, check=False,
                               text=True, capture_output=True, timeout=1800)
        if audit.returncode == 0:
            return "READY", None
        full_script = self.paths.project_root / "scripts/knowledge_index_full.sh"
        if full_script.is_symlink() or not full_script.is_file():
            raise RuntimeError("Initial index wrapper is missing or unsafe")
        payload = self.cli.json([
            "cron", "add", "--name", "Qwen local knowledge initial full index", "--at", "+10s",
            "--command-argv", json.dumps([str(full_script)], separators=(",", ":")),
            "--command-cwd", str(self.paths.project_root), "--timeout-seconds", "86400",
            "--no-output-timeout-seconds", "1800", "--output-max-bytes", "65536",
            "--declaration-key", "openclaw-lancedb-knowledge-local-initial-v1",
            "--delete-after-run", "--no-deliver", "--json",
        ])
        job_id = payload.get("id") or payload.get("job", {}).get("id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("OpenClaw initial index job did not return a job id")
        return "INDEX_BUILDING", job_id

    def verify(self) -> dict[str, Any]:
        manifest = self.store.read()
        plugin = self.cli.json(["plugins", "inspect", PLUGIN_ID, "--runtime", "--json"])
        skill = self.cli.json(["skills", "info", SKILL_ID, "--agent", self.agent, "--json"])
        jobs_payload = self.cli.json(["cron", "list", "--all", "--json"])
        jobs = jobs_payload.get("jobs", jobs_payload) if isinstance(jobs_payload, dict) else jobs_payload
        if not isinstance(jobs, list):
            raise RuntimeError("Cron verification returned an unexpected schema")
        matching = [job for job in jobs if job.get("declarationKey") == CRON_DECLARATION_KEY]
        plugin_text = json.dumps(plugin, sort_keys=True)
        skill_text = json.dumps(skill, sort_keys=True)
        if TOOL_NAME not in plugin_text or PLUGIN_ID not in plugin_text:
            raise RuntimeError("local_knowledge_search tool owner is not loaded")
        if SKILL_ID not in skill_text or not (skill.get("eligible") is True or '"eligible": true' in skill_text.lower()):
            raise RuntimeError("Local knowledge skill is not eligible")
        if len(matching) != 1:
            raise RuntimeError("Incremental cron declaration is missing or duplicated")
        gateway = self.cli.json(["gateway", "status", "--require-rpc", "--json"])
        return {
            "ok": True, "phase": manifest.get("phase"), "pluginLoaded": True, "skillEligible": True,
            "incrementalCronUnique": True, "gateway": bool(gateway), "indexState": manifest.get("indexState"),
        }

    def integrate(self, runtime_manifest: dict[str, Any]) -> dict[str, Any]:
        with self._integration_lock():
            return self._integrate_locked(runtime_manifest)

    def _integrate_locked(self, runtime_manifest: dict[str, Any]) -> dict[str, Any]:
        if self.store.manifest_path.is_file() and not self.store.manifest_path.is_symlink():
            existing = self.store.read()
            if existing.get("phase") == "committed":
                return {"status": existing.get("indexState"), "transaction": "already_committed", **self.verify()}
            if existing.get("phase") != "rolled_back":
                raise RuntimeError("An unfinished OpenClaw integration transaction requires rollback")
        transaction = self.begin()
        try:
            transaction["phase"] = "staging"
            transaction["runtimePort"] = int(runtime_manifest["runtimePort"])
            transaction["projectCreated"] = self.bootstrap_project(runtime_manifest)
            if not transaction["projectCreated"]:
                self.synchronize_project_runtime()
            self.store.write(transaction)

            self.configure_openclaw(self._allowed_projects())
            self.install_launchd_plist(runtime_manifest)
            transaction["ownedAssets"] = [PLUGIN_ID, SKILL_ID, LAUNCHD_LABEL, CRON_DECLARATION_KEY]
            transaction["phase"] = "activating"
            self.store.write(transaction)

            self.activate_launchd()
            transaction["cronId"] = self.create_incremental_cron()
            transaction["disabledGeminiJobs"] = self.disable_owned_gemini_jobs()
            transaction["indexState"], transaction["initialIndexJobId"] = self.mark_ready_or_schedule_build()
            self.cli.run(["config", "validate", "--json"])
            config = Path(transaction["configPath"])
            transaction["postConfigSha256"] = self._sha256_config(config)
            transaction["phase"] = "restarting_gateway"
            self.store.write(transaction)
            self.cli.run(["gateway", "restart", "--safe", "--json"], timeout=300)
            transaction["phase"] = "committed"
            self.store.write(transaction)
            verification = self.verify()
            return {"status": transaction["indexState"], "transaction": "committed", **verification}
        except Exception:
            transaction["phase"] = "failed"
            self.store.write(transaction)
            try:
                self._rollback_locked(require_exact_post_config=False)
            except Exception:
                transaction["phase"] = "rollback_failed"
                self.store.write(transaction)
            raise

    def _restore_regular_file(self, source: Path, target: Path) -> None:
        if source.is_symlink() or not source.is_file() or target.is_symlink():
            raise RuntimeError("Rollback file boundary is unsafe")
        temporary = target.with_suffix(target.suffix + ".restore-tmp")
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("Rollback staging file already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(temporary, target)

    def _restore_config_file(self, source: Path, target: Path, *, expected_sha256: str,
                             expected_run_identity: tuple[int, int], expected_marker_sha256: str) -> None:
        source = Path(os.path.abspath(source))
        target = Path(os.path.abspath(target))
        self._snapshot_run_from_backup(source)
        temporary_name = target.name + ".restore-tmp"
        source_fd: int | None = None
        target_fd: int | None = None
        temporary_created = False
        try:
            with self._open_private_directory(source.parent) as source_parent_fd, \
                    self._open_private_directory(target.parent) as target_parent_fd:
                self._validate_restricted_directory(os.fstat(source_parent_fd))
                source_parent = os.fstat(source_parent_fd)
                if (source_parent.st_dev, source_parent.st_ino) != expected_run_identity:
                    raise RuntimeError("Rollback snapshot run identity changed")
                self._verify_snapshot_marker(source_parent_fd, expected_marker_sha256)
                source_fd = os.open(
                    source.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                    dir_fd=source_parent_fd,
                )
                self._validate_private_config(os.fstat(source_fd))
                current_fd = os.open(
                    target.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                    dir_fd=target_parent_fd,
                )
                try:
                    self._validate_private_config(os.fstat(current_fd))
                finally:
                    os.close(current_fd)
                target_fd = os.open(
                    temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=target_parent_fd,
                )
                temporary_created = True
                try:
                    with os.fdopen(source_fd, "rb", closefd=True) as input_handle, \
                            os.fdopen(target_fd, "wb", closefd=True) as output_handle:
                        source_fd = None
                        target_fd = None
                        before = os.fstat(input_handle.fileno())
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                            output_handle.write(chunk)
                        after = os.fstat(input_handle.fileno())
                        self._assert_stable_file(before, after)
                        if digest.hexdigest() != expected_sha256:
                            raise RuntimeError("Rollback config snapshot hash mismatch")
                        output_handle.flush()
                        os.fsync(output_handle.fileno())
                    os.replace(
                        temporary_name, target.name,
                        src_dir_fd=target_parent_fd, dst_dir_fd=target_parent_fd,
                    )
                    temporary_created = False
                    os.fsync(target_parent_fd)
                except Exception:
                    if temporary_created:
                        try:
                            os.unlink(temporary_name, dir_fd=target_parent_fd)
                        except FileNotFoundError:
                            pass
                    raise
        except OSError as error:
            raise RuntimeError("Rollback config path is missing or unsafe") from error
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if target_fd is not None:
                os.close(target_fd)

    def _verify_config_snapshot(self, source: Path, *, expected_sha256: str,
                                expected_run_identity: tuple[int, int],
                                expected_marker_sha256: str) -> None:
        source = Path(os.path.abspath(source))
        self._snapshot_run_from_backup(source)
        source_fd: int | None = None
        try:
            with self._open_private_directory(source.parent) as source_parent_fd:
                self._validate_restricted_directory(os.fstat(source_parent_fd))
                source_parent = os.fstat(source_parent_fd)
                if (source_parent.st_dev, source_parent.st_ino) != expected_run_identity:
                    raise RuntimeError("Rollback snapshot run identity changed")
                self._verify_snapshot_marker(source_parent_fd, expected_marker_sha256)
                source_fd = os.open(
                    source.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                    dir_fd=source_parent_fd,
                )
                self._validate_private_config(os.fstat(source_fd))
                with os.fdopen(source_fd, "rb", closefd=True) as handle:
                    source_fd = None
                    before = os.fstat(handle.fileno())
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                    after = os.fstat(handle.fileno())
                    self._assert_stable_file(before, after)
                if digest.hexdigest() != expected_sha256:
                    raise RuntimeError("Rollback config snapshot hash mismatch")
        except OSError as error:
            raise RuntimeError("Rollback config snapshot is missing or unsafe") from error
        finally:
            if source_fd is not None:
                os.close(source_fd)

    def rollback(self, *, require_exact_post_config: bool = True) -> dict[str, Any]:
        with self._integration_lock():
            return self._rollback_locked(require_exact_post_config=require_exact_post_config)

    def _rollback_locked(self, *, require_exact_post_config: bool = True) -> dict[str, Any]:
        transaction = self.store.read()
        config = Path(transaction["configPath"])
        run_dev = transaction.get("snapshotRunDev")
        run_ino = transaction.get("snapshotRunIno")
        pre_config_sha256 = transaction.get("preConfigSha256")
        marker_sha256 = transaction.get("snapshotRunMarkerSha256")
        if type(run_dev) is not int or type(run_ino) is not int \
                or not isinstance(pre_config_sha256, str) or len(pre_config_sha256) != 64 \
                or not isinstance(marker_sha256, str) or len(marker_sha256) != 64:
            raise RuntimeError("Rollback snapshot integrity metadata is missing")
        snapshot_path = Path(transaction["configBackupPath"])
        self._verify_config_snapshot(
            snapshot_path,
            expected_sha256=pre_config_sha256,
            expected_run_identity=(run_dev, run_ino),
            expected_marker_sha256=marker_sha256,
        )
        if require_exact_post_config and transaction.get("postConfigSha256") and \
                self._sha256_config(config) != transaction["postConfigSha256"]:
            raise RuntimeError("OpenClaw config drifted after integration; refusing automatic rollback")
        if transaction.get("cronId"):
            self.cli.run(["cron", "rm", str(transaction["cronId"])], check=False)
        if transaction.get("initialIndexJobId"):
            self.cli.run(["cron", "rm", str(transaction["initialIndexJobId"])], check=False)
        for job in transaction.get("disabledGeminiJobs", []):
            if job.get("wasEnabled"):
                self.cli.run(["cron", "enable", str(job["id"])], check=False)
        self.deactivate_launchd()
        plist_backup = Path(transaction["plistBackupPath"])
        if transaction.get("plistExisted"):
            self._restore_regular_file(plist_backup, self.paths.launchd_plist)
            self._launchctl(["bootstrap", f"gui/{os.getuid()}", str(self.paths.launchd_plist)], check=False)
        else:
            self.paths.launchd_plist.unlink(missing_ok=True)
        self.cli.run(["plugins", "uninstall", PLUGIN_ID, "--force"], check=False)
        skill_target = Path(transaction["skillTargetPath"])
        if skill_target.exists():
            if skill_target.is_symlink():
                raise RuntimeError("Installed skill became a symbolic link; refusing rollback deletion")
            shutil.rmtree(skill_target)
        if transaction.get("skillExisted"):
            shutil.copytree(Path(transaction["skillBackupPath"]), skill_target)
        project_backup = Path(transaction["projectBackupPath"])
        if transaction.get("projectExisted"):
            for relative in (Path("src"), Path("scripts"), Path("package.json"), Path("package-lock.json")):
                target = self.paths.project_root / relative
                if target.is_symlink():
                    raise RuntimeError("Qwen project runtime became a symbolic link; refusing rollback")
                if target.exists():
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                source = project_backup / relative
                if source.exists():
                    shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        elif transaction.get("projectCreated"):
            if self.paths.project_root.is_symlink():
                raise RuntimeError("Created Qwen project became a symbolic link; refusing rollback")
            shutil.rmtree(self.paths.project_root)
        self._restore_config_file(
            snapshot_path, config,
            expected_sha256=pre_config_sha256,
            expected_run_identity=(run_dev, run_ino),
            expected_marker_sha256=marker_sha256,
        )
        self.cli.run(["config", "validate", "--json"])
        self.cli.run(["gateway", "restart", "--safe", "--json"], timeout=300, check=False)
        transaction["phase"] = "rolled_back"
        self.store.write(transaction)
        return {"ok": True, "status": "ROLLED_BACK"}

    def uninstall(self) -> dict[str, Any]:
        result = self.rollback(require_exact_post_config=True)
        result["preservedProject"] = True
        result["preservedRuntime"] = True
        return result
