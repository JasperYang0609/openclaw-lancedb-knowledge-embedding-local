from __future__ import annotations

import hashlib
import ctypes
import errno
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
import fcntl
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .asset_recovery import (
    AssetIdentity,
    inspect_asset_at,
    publish_noreplace_at,
    purge_exact_at,
    quarantine_exact_at,
    stage_copy_at,
)
from .launchd import LAUNCHD_LABEL, build_launchd_plist

PLUGIN_ID = "openclaw-lancedb-knowledge-local"
SKILL_ID = "openclaw-lancedb-knowledge-local"
TOOL_NAME = "local_knowledge_search"
SNAPSHOT_MARKER_NAME = ".snapshot-run-id"
CRON_DECLARATION_KEY = "openclaw-lancedb-knowledge-local-incremental-v1"
SNAPSHOT_CRON_DECLARATION_KEY = "openclaw-lancedb-knowledge-local-snapshot-v1"
INITIAL_CRON_DECLARATION_KEY = "openclaw-lancedb-knowledge-local-initial-v1"
LEGACY_SNAPSHOT_DECLARATION_KEY = "lancedb-daily-desktop-backup-permanent"
GEMINI_DECLARATION_KEY = "openclaw-lancedb-knowledge-gemini-incremental-v1"
SCHEMA_VERSION = 1
INTEGRATION_CONTRACT_VERSION = 3
OWNERSHIP_SCHEMA = "qwen-local-openclaw.v3"
CRON_CONTRACT_HASH_VERSION = 2
ASSET_ROLLBACK_RECEIPT_SCHEMA = "qwen-local.rollback-assets.v1"
SNAPSHOT_CONTRACT = "qwen-local-verified-snapshot.v1"
HEALTH_RECEIPT_SCHEMA = "backup-health-component.v1"
HEALTH_RECEIPT_MAX_AGE_SECONDS = 36 * 60 * 60
HEALTH_RECEIPT_MAX_BYTES = 16 * 1024
HEALTH_RECEIPT_MAX_ITEMS = 20
LAUNCHD_RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0, 4.0)
FORBIDDEN_MANIFEST_KEYS = {"token", "secret", "password", "credential", "apiKey", "api_key", "query", "corpus", "vector"}
INCREMENTAL_CRON_DESCRIPTION = "Incrementally refresh the installer-owned local Qwen knowledge index."
SNAPSHOT_CRON_DESCRIPTION = "Create and verify the installer-owned local Qwen recovery snapshot."
INITIAL_CRON_DESCRIPTION = "Build the installer-owned local Qwen knowledge index once."
LEGACY_SNAPSHOT_NAME = "LanceDB 知識庫每日桌面備份（保留30天）"
LEGACY_SNAPSHOT_DESCRIPTION = (
    "每日 06:30 增量索引後建立 daily-YYYY-MM-DD checksummed 桌面快照；"
    "永久保留，不自動刪除；同日重跑只驗證。"
)
MANAGED_CRON_KEYS = {
    CRON_DECLARATION_KEY,
    SNAPSHOT_CRON_DECLARATION_KEY,
    INITIAL_CRON_DECLARATION_KEY,
}
CRON_DEFINITION_FIELDS = (
    "id", "name", "description", "enabled", "declarationKey", "schedule", "payload",
    "delivery", "failureAlert", "sessionTarget", "sessionKey", "agentId", "deleteAfterRun",
    "wakeMode", "displayName", "owner", "trigger",
)
LEGACY_V3_CRON_DEFINITION_FIELDS = CRON_DEFINITION_FIELDS[:-4]
CRON_VOLATILE_FIELDS = {
    "createdAtMs", "updatedAtMs", "state", "status", "lastDelivered",
    "lastDeliveryError", "lastDeliveryStatus", "lastFailureNotificationDeliveryStatus",
    "lastRunAtMs", "lastRunError", "lastRunStatus", "nextRunAtMs",
}
DISCORD_CHANNEL_TARGET_RE = re.compile(r"^channel:[1-9][0-9]{16,21}$")
SAFE_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_CRON_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SENSITIVE_KEY_MARKERS = (
    "token", "secret", "password", "credential", "apikey", "accesskey", "privatekey",
    "authorization", "bearer", "cookie", "sessioncookie",
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)(?:^|\s)bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?:^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:^|[^A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?:^|[^A-Za-z0-9])AKIA[0-9A-Z]{16}(?:$|[^A-Za-z0-9])"),
    re.compile(r"(?:^|[^A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)

INDEX_LOCK_STAGE_RE = re.compile(r"^\.index\.lock\.install-[0-9a-f]{32}$")
SNAPSHOT_LOCK_STAGE_RE = re.compile(r"^\.snapshot-run\.lock\.install-[0-9a-f]{32}$")
PROJECT_ROOT_STAGE_RE = re.compile(r"^\.qwen-project-root\.install-[0-9a-f]{32}$")
SNAPSHOT_ROOT_STAGE_RE = re.compile(r"^\.qwen-snapshot-root\.install-[0-9a-f]{32}$")
QUARANTINE_RE = re.compile(r"^\.qwen-recovery-quarantine-[0-9a-f]{32}$")
CAPABILITY_PROBE_STAGE_RE = re.compile(r"^\.qwen-capability-probe-[0-9a-f]{32}$")
CAPABILITY_PROBE_FINAL_RE = re.compile(r"^\.qwen-capability-probe-published-[0-9a-f]{32}$")
ASSET_RESTORE_STAGE_RE = re.compile(r"^\.qwen-asset-restore-[0-9a-f]{32}$")
ASSET_INSTALL_STAGE_RE = re.compile(r"^\.qwen-asset-install-[0-9a-f]{32}$")
ASSET_PARENT_STAGE_RE = re.compile(r"^\.qwen-asset-parent-install-[0-9a-f]{32}$")
ROLLBACK_ASSET_IDS = (
    "plugin",
    "skill",
    "project.src",
    "project.scripts",
    "project.package_json",
    "project.package_lock",
)


def _valid_numeric_cron_expression(expression: str) -> bool:
    """Accept the bounded five-field numeric cron subset used by managed jobs."""
    if not expression or expression != expression.strip() \
            or expression.startswith("-") or "\x00" in expression \
            or len(expression) > 512:
        return False
    fields = expression.split(" ")
    if len(fields) != 5 or any(not field for field in fields):
        return False
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def valid_atom(atom: str, minimum: int, maximum: int) -> bool:
        base, separator, step_text = atom.partition("/")
        if separator:
            if "/" in step_text or not step_text.isascii() or not step_text.isdigit():
                return False
            step = int(step_text)
            if not 1 <= step <= maximum - minimum + 1:
                return False
        if base == "*":
            return True
        if "-" in base:
            start_text, dash, end_text = base.partition("-")
            if not dash or "-" in end_text:
                return False
            if not start_text.isascii() or not start_text.isdigit() \
                    or not end_text.isascii() or not end_text.isdigit():
                return False
            start = int(start_text)
            end = int(end_text)
            return minimum <= start <= end <= maximum
        if separator:
            return False
        if not base.isascii() or not base.isdigit():
            return False
        return minimum <= int(base) <= maximum

    return all(
        all(valid_atom(atom, minimum, maximum) for atom in field.split(","))
        for field, (minimum, maximum) in zip(fields, bounds)
    )


def _rename_noreplace_at(source_fd: int, source: str, target_fd: int, target: str) -> None:
    """Atomically publish one same-filesystem path without replacing an existing target."""
    for value in (source, target):
        if not value or value in {".", ".."} or "/" in value or "\x00" in value:
            raise RuntimeError("Atomic publication path is unsafe")
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        operation = getattr(libc, "renameatx_np", None)
        flag = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        operation = getattr(libc, "renameat2", None)
        flag = 0x00000001  # RENAME_NOREPLACE
    else:
        operation = None
        flag = 0
    if operation is None:
        raise RuntimeError("Atomic no-replace publication is unavailable on this platform")
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(source_fd, source_bytes, target_fd, target_bytes, flag) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), target)
    if error_number in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
        raise RuntimeError("Atomic no-replace publication is unavailable on this filesystem")
    raise OSError(error_number, os.strerror(error_number), target)


class IntegrationRollbackIncomplete(RuntimeError):
    """Integration failed and its automatic rollback could not be verified complete."""

    def __init__(self, original_error: Exception, rollback_error: Exception) -> None:
        super().__init__(
            "Integration failed and automatic rollback did not complete; state is recoverable"
        )
        self.original_error = original_error
        self.rollback_error = rollback_error
        self.recovery_state = "automatic_rollback_incomplete"


class ActivationFailSafeIncomplete(RuntimeError):
    """An armed transaction could not disable every exact managed cron target."""

    def __init__(self, compensation_error: Exception) -> None:
        super().__init__(
            "Activation fail-safe compensation did not complete; explicit recovery is required"
        )
        self.compensation_error = compensation_error
        self.recovery_state = "activation_fail_safe_incomplete"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_no_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
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


def _sensitive_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return str(value) in FORBIDDEN_MANIFEST_KEYS or any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_sensitive_key(key) or _contains_forbidden_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS)
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
        temporary = self.state_root / f".transaction.{uuid.uuid4().hex}.tmp"
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
            directory_fd = os.open(
                self.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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


def resolve_tool_allowlist_update(tools_allow: Any, tools_also_allow: Any,
                                  value: str) -> tuple[str | None, list[str] | None]:
    """Select the active explicit tool allowlist without broadening policy semantics."""
    if tools_allow is not None:
        return "tools.allow", merge_allowlist(tools_allow, value)
    if tools_also_allow is not None:
        return "tools.alsoAllow", merge_allowlist(tools_also_allow, value)
    return None, None


def build_cron_add_args(*, project_root: Path, incremental_script: Path,
                        schedule: str = "30 6 * * *", timezone: str = "Asia/Taipei",
                        ownership_manifest: Path | None = None,
                        python_path: Path | None = None,
                        disabled: bool = False) -> list[str]:
    project = Path(project_root).resolve(strict=False)
    script = Path(incremental_script).resolve(strict=False)
    if project not in script.parents or script.name != "knowledge_index_incremental.sh":
        raise ValueError("Incremental script must be the managed project wrapper")
    argv = [str(script)]
    extra: list[str] = []
    if ownership_manifest is not None:
        manifest = Path(ownership_manifest).resolve(strict=False)
        if not manifest.is_absolute():
            raise ValueError("Ownership manifest must be absolute")
        argv.append(str(manifest))
        extra.extend(["--command-env", f"QWEN_OWNERSHIP_MANIFEST={manifest}"])
    if python_path is not None:
        python = Path(python_path).resolve(strict=False)
        if not python.is_absolute():
            raise ValueError("Python executable must be absolute")
        extra.extend(["--command-env", f"QWEN_PYTHON={python}"])
    args = [
        "cron", "add", "--name", "Qwen local knowledge incremental index", "--cron", schedule,
        "--tz", timezone, "--exact", "--command-argv", json.dumps(argv, separators=(",", ":")),
        "--command-cwd", str(project), "--timeout-seconds", "7200",
        "--no-output-timeout-seconds", "900", "--output-max-bytes", "65536",
        *extra, "--declaration-key", CRON_DECLARATION_KEY, "--wake", "now",
        "--no-deliver", "--json",
    ]
    if disabled:
        args.insert(-1, "--disabled")
    return args


def build_snapshot_cron_add_args(*, project_root: Path, snapshot_wrapper: Path,
                                 ownership_manifest: Path, python_path: Path,
                                 schedule: str = "50 6 * * *",
                                 timezone: str = "Asia/Taipei",
                                 disabled: bool = True) -> list[str]:
    project = Path(project_root).resolve(strict=False)
    wrapper = Path(snapshot_wrapper).resolve(strict=False)
    manifest = Path(ownership_manifest).resolve(strict=False)
    python = Path(python_path).resolve(strict=False)
    if project not in wrapper.parents or wrapper.name != "run_verified_snapshot.py":
        raise ValueError("Snapshot wrapper must be the managed project wrapper")
    if not manifest.is_absolute() or not python.is_absolute():
        raise ValueError("Snapshot cron paths must be absolute")
    args = [
        "cron", "add", "--name", "Qwen local verified recovery snapshot", "--cron", schedule,
        "--tz", timezone, "--exact", "--command-argv",
        json.dumps([str(python), str(wrapper), "--ownership-manifest", str(manifest)], separators=(",", ":")),
        "--command-cwd", str(project), "--timeout-seconds", "7200",
        "--no-output-timeout-seconds", "3600", "--output-max-bytes", "16384",
        "--declaration-key", SNAPSHOT_CRON_DECLARATION_KEY, "--wake", "now",
        "--no-deliver", "--json",
    ]
    if disabled:
        args.insert(-1, "--disabled")
    return args


@dataclass(frozen=True)
class ManagedCronSpec:
    key: str
    name: str
    description: str
    schedule: str
    timezone: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    no_output_timeout_seconds: int
    output_max_bytes: int
    report_channel: str
    report_to: str
    report_account_id: str
    session_target: str = "isolated"
    command_env: tuple[tuple[str, str], ...] = ()

    def add_args(
        self, *, disabled: bool = True, description: str | None = None,
    ) -> list[str]:
        effective_description = self.description if description is None else description
        args = [
            "cron", "add", "--name", self.name, "--description", effective_description,
            "--session", self.session_target, "--cron", self.schedule,
            "--tz", self.timezone, "--exact", "--command-argv",
            json.dumps(list(self.argv), separators=(",", ":")),
            "--command-cwd", self.cwd, "--timeout-seconds", str(self.timeout_seconds),
            "--no-output-timeout-seconds", str(self.no_output_timeout_seconds),
            "--output-max-bytes", str(self.output_max_bytes),
        ]
        for key, value in self.command_env:
            args.extend(["--command-env", f"{key}={value}"])
        args.extend(["--declaration-key", self.key, "--wake", "now", "--no-deliver"])
        if disabled:
            args.append("--disabled")
        args.append("--json")
        return args

    def alert_args(self, job_id: str) -> list[str]:
        args = [
            "cron", "edit", job_id, "--failure-alert", "--failure-alert-after", "1",
            "--failure-alert-cooldown", "1h", "--failure-alert-exclude-skipped",
            "--failure-alert-mode", "announce", "--failure-alert-channel", self.report_channel,
            "--failure-alert-to", self.report_to,
            "--failure-alert-account-id", self.report_account_id,
            "--no-deliver", "--disable",
        ]
        return args


@dataclass(frozen=True)
class ApprovedDisabledCronCollision:
    """One operator-reviewed disabled legacy collision that remains customer-owned."""

    job_id: str
    contract_sha256: str
    role: str

    def __post_init__(self) -> None:
        if type(self.job_id) is not str or not SAFE_CRON_JOB_ID_RE.fullmatch(self.job_id):
            raise ValueError("Approved disabled collision job id is invalid")
        if type(self.contract_sha256) is not str \
                or not re.fullmatch(r"[0-9a-f]{64}", self.contract_sha256):
            raise ValueError("Approved disabled collision SHA-256 is invalid")
        if type(self.role) is not str or self.role != "incremental":
            raise ValueError("Approved disabled collision role must be incremental")

    def receipt(self) -> dict[str, str]:
        return {
            "jobId": self.job_id,
            "contractSha256": self.contract_sha256,
            "role": self.role,
        }


@dataclass(frozen=True)
class RollbackAssetSpec:
    asset_id: str
    target: Path
    backup_relative: Path
    kind: str
    mutation_field: str

    def __post_init__(self) -> None:
        if self.asset_id not in ROLLBACK_ASSET_IDS:
            raise ValueError("Rollback asset identity is unsupported")
        if self.kind not in {"directory", "file"}:
            raise ValueError("Rollback asset kind is unsupported")
        if self.backup_relative.is_absolute() or ".." in self.backup_relative.parts:
            raise ValueError("Rollback asset backup identity is unsafe")


def _job_argv(job: dict[str, Any]) -> list[str]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    current_argv = payload.get("argv")
    if isinstance(current_argv, list) and all(isinstance(item, str) for item in current_argv):
        return current_argv
    command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
    legacy_argv = command.get("argv")
    return legacy_argv if isinstance(legacy_argv, list) and all(isinstance(item, str) for item in legacy_argv) else []


def _argv_targets_exact_script(argv: list[str], expected_script: Path) -> bool:
    expected = expected_script.resolve(strict=False)

    def same_script(raw: str) -> bool:
        candidate = Path(raw).expanduser()
        return candidate.is_absolute() and candidate.resolve(strict=False) == expected

    if any(same_script(raw) for raw in argv):
        return True
    safe_shells = {"sh", "/bin/sh", "bash", "/bin/bash", "zsh", "/bin/zsh"}
    if len(argv) != 3 or argv[0] not in safe_shells or argv[1] != "-lc":
        return False
    try:
        lexer = shlex.shlex(argv[2], posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        raise RuntimeError("Cron shell command is not safely parseable")
    return any(same_script(token) for token in tokens)


def _job_targets_exact_script(job: dict[str, Any], expected_script: Path) -> bool:
    return _argv_targets_exact_script(_job_argv(job), expected_script)


def _job_targets_snapshot_wrapper(job: dict[str, Any], expected_wrapper: Path) -> bool:
    argv = _job_argv(job)
    if any(
        Path(raw).expanduser().is_absolute()
        and Path(raw).expanduser().resolve(strict=False) == expected_wrapper.resolve(strict=False)
        for raw in argv
    ):
        return True
    return _argv_targets_exact_script(argv, expected_wrapper)


def _cron_jobs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("OpenClaw cron inventory lacks a completeness envelope")
    if payload.get("truncated") is True or payload.get("hasMore") is not False \
            or payload.get("nextCursor") or payload.get("offset") not in (None, 0) \
            or payload.get("nextOffset") is not None:
        raise RuntimeError("OpenClaw cron inventory is incomplete")
    jobs = payload.get("jobs")
    total = payload.get("total")
    if type(total) is not int:
        raise RuntimeError("OpenClaw cron inventory total is missing")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise RuntimeError("OpenClaw cron list returned an unexpected schema")
    if len(jobs) != total:
        raise RuntimeError("OpenClaw cron inventory count is incomplete")
    ids: list[str] = []
    keys: list[str] = []
    for job in jobs:
        job_id = job.get("id")
        if not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id):
            raise RuntimeError("OpenClaw cron inventory contains an invalid job id")
        ids.append(job_id)
        key = job.get("declarationKey")
        if key not in (None, ""):
            if not isinstance(key, str):
                raise RuntimeError("OpenClaw cron inventory contains an invalid declaration key")
            keys.append(key)
    if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
        raise RuntimeError("OpenClaw cron inventory contains duplicate identities")
    return jobs


def _job_env(job: dict[str, Any]) -> dict[str, str]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    value = payload.get("env", {})
    return value if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()) else {}


def _no_delivery_contract(value: Any) -> bool:
    """Accept only the two no-delivery shapes emitted by supported OpenClaw CLIs."""
    return value in (
        {"mode": "none"},
        {"mode": "none", "channel": "last"},
    )


def _default_cron_behavior_contract(job: dict[str, Any]) -> bool:
    """Accept only cron behavior fields that this installer can round-trip exactly."""
    return (
        job.get("wakeMode", "now") == "now"
        and job.get("displayName") is None
        and job.get("owner") is None
        and job.get("trigger") is None
    )


def _known_cron_top_level_contract(job: dict[str, Any]) -> bool:
    """Reject newly persisted behavior fields until their semantics are reviewed."""
    return not (set(job) - set(CRON_DEFINITION_FIELDS) - CRON_VOLATILE_FIELDS)


def _job_matches_spec(job: dict[str, Any], spec: ManagedCronSpec, *, require_enabled: bool) -> bool:
    try:
        _job_definition(job)
    except RuntimeError:
        return False
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
    alert = job.get("failureAlert") if isinstance(job.get("failureAlert"), dict) else {}
    if job.get("enabled") is not require_enabled:
        return False
    expected_env = dict(spec.command_env)
    expected_payload_keys = {
        "kind", "argv", "cwd", "timeoutSeconds", "noOutputTimeoutSeconds", "outputMaxBytes",
    }
    if expected_env:
        expected_payload_keys.add("env")
    expected_alert = {
        "after": 1,
        "cooldownMs": 3600000,
        "includeSkipped": False,
        "mode": "announce",
        "channel": spec.report_channel,
        "to": spec.report_to,
        "accountId": spec.report_account_id,
    }
    return (
        _known_cron_top_level_contract(job)
        and job.get("declarationKey") == spec.key
        and job.get("name") == spec.name
        and job.get("description") == spec.description
        and _default_cron_behavior_contract(job)
        and job.get("sessionTarget") == spec.session_target
        and job.get("sessionKey") is None
        and job.get("agentId") is None
        and (job.get("deleteAfterRun") is None or job.get("deleteAfterRun") is False)
        and schedule == {
            "kind": "cron", "expr": spec.schedule, "tz": spec.timezone, "staggerMs": 0,
        }
        and set(payload) == expected_payload_keys
        and payload.get("kind") == "command"
        and _job_argv(job) == list(spec.argv)
        and payload.get("cwd") == spec.cwd
        and payload.get("timeoutSeconds") == spec.timeout_seconds
        and payload.get("noOutputTimeoutSeconds") == spec.no_output_timeout_seconds
        and payload.get("outputMaxBytes") == spec.output_max_bytes
        and "toolsAllow" not in payload
        and _job_env(job) == expected_env
        and _no_delivery_contract(delivery)
        and alert == expected_alert
    )


def _legacy_snapshot_shell_command(*, project_root: Path, snapshot_root: Path, timezone_name: str) -> str:
    project = str(project_root)
    snapshot = str(snapshot_root)
    return (
        f"set -eu; day=$(TZ={timezone_name} date +%F); project={project}; backup=\"{snapshot}\"; "
        "helper=\"$project/scripts/snapshot_knowledge_assets.py\"; name=\"daily-$day\"; "
        "target=\"$backup/snapshots/$name\"; "
        "verify_log=\"$project/reports/cron-logs/snapshot-verify-$day.log\"; "
        "mkdir -p \"$project/reports/cron-logs\"; wait_count=0; "
        "while [ -d \"$project/data/index.lock\" ]; do wait_count=$((wait_count+1)); "
        "[ \"$wait_count\" -le 120 ] || { echo \"index lock did not clear within 30 minutes\" >&2; exit 75; }; "
        "sleep 15; done; audit=$(cd \"$project\" && node src/cli.js audit --json); "
        "rows=$(printf \"%s\" \"$audit\" | jq -er \".rows\"); "
        "indexed_at=$(jq -er \".updatedAt\" \"$project/data/index-state.json\"); "
        "if [ -d \"$target\" ]; then if python3 \"$helper\" --verify-snapshot \"$target\" "
        "--expected-snapshot-root \"$backup\" --require-after \"$indexed_at\" --restore-canary --verify-db "
        "--table-name knowledge_chunks_qwen_local_768 --expected-row-count \"$rows\" --retention-days 30 "
        "--retention-reference-date \"$day\" --transient-retention-days 7 --transient-max-count 10 "
        ">\"$verify_log\" 2>&1; then cat \"$verify_log\"; else latest_repair=$(find \"$backup/snapshots\" "
        "-mindepth 1 -maxdepth 1 -type d -name \"repair-$day-*-post-index\" -print | LC_ALL=C sort | "
        "tail -n 1 || true); if [ -n \"$latest_repair\" ] && python3 \"$helper\" --verify-snapshot "
        "\"$latest_repair\" --expected-snapshot-root \"$backup\" --require-after \"$indexed_at\" "
        "--restore-canary --verify-db --table-name knowledge_chunks_qwen_local_768 --expected-row-count "
        "\"$rows\" --retention-days 30 --retention-reference-date \"$day\" --transient-retention-days 7 "
        "--transient-max-count 10 >\"$verify_log\" 2>&1; then cat \"$verify_log\"; else echo "
        "\"daily snapshot is stale or invalid; creating an immutable repair snapshot\"; "
        f"repair_name=\"repair-$day-$(TZ={timezone_name} date +%H%M%S)-post-index\"; "
        "python3 \"$helper\" --project-dir \"$project\" --backup-root \"$backup\" --snapshot-name "
        "\"$repair_name\" --require-after \"$indexed_at\" --restore-canary --verify-db --table-name "
        "knowledge_chunks_qwen_local_768 --expected-row-count \"$rows\" --retention-days 30 "
        "--retention-reference-date \"$day\" --transient-retention-days 7 --transient-max-count 10; "
        "fi; fi; else python3 \"$helper\" --project-dir \"$project\" --backup-root \"$backup\" "
        "--snapshot-name \"$name\" --require-after \"$indexed_at\" --restore-canary --verify-db "
        "--table-name knowledge_chunks_qwen_local_768 --expected-row-count \"$rows\" --retention-days 30 "
        "--retention-reference-date \"$day\" --transient-retention-days 7 --transient-max-count 10; fi"
    )


def _cron_contract_payload(job: dict[str, Any], *, include_id: bool) -> dict[str, Any]:
    fields = CRON_DEFINITION_FIELDS if include_id else tuple(
        key for key in CRON_DEFINITION_FIELDS if key != "id"
    )
    contract = {key: job.get(key) for key in fields}
    for key in sorted(set(job) - set(CRON_DEFINITION_FIELDS) - CRON_VOLATILE_FIELDS):
        contract[key] = job[key]
    if "wakeMode" not in job:
        contract["wakeMode"] = "now"
    if contract.get("deleteAfterRun") is None or contract.get("deleteAfterRun") is False:
        contract["deleteAfterRun"] = False
    if _no_delivery_contract(contract.get("delivery")):
        contract["delivery"] = {"mode": "none"}
    return contract


def _job_contract_hash(job: dict[str, Any], *, include_id: bool = False) -> str:
    encoded = json.dumps(
        _cron_contract_payload(job, include_id=include_id),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_v3_job_contract_hash(
    job: dict[str, Any], *, include_id: bool = False,
) -> str:
    fields = LEGACY_V3_CRON_DEFINITION_FIELDS if include_id else tuple(
        key for key in LEGACY_V3_CRON_DEFINITION_FIELDS if key != "id"
    )
    encoded = json.dumps(
        {key: job.get(key) for key in fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _staging_contract_hash(job: dict[str, Any]) -> str:
    """Hash a staged cron contract with only the supported no-delivery normalization."""
    contract = _cron_contract_payload(job, include_id=False)
    if _no_delivery_contract(contract.get("delivery")):
        contract["delivery"] = {"mode": "none"}
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job_definition(job: dict[str, Any]) -> dict[str, Any]:
    """Persist a bounded, restorable definition allowlist, never arbitrary cron payload data."""
    if not isinstance(job, dict) \
            or set(job) - set(CRON_DEFINITION_FIELDS) - CRON_VOLATILE_FIELDS:
        raise RuntimeError("Owned cron definition has unsupported top-level fields")
    try:
        serialized = json.dumps(
            _cron_contract_payload(job, include_id=True), ensure_ascii=False,
        )
        encoded = serialized.encode("utf-8")
        if len(encoded) > 128 * 1024:
            raise ValueError("definition exceeds the rollback receipt size limit")
        raw = json.loads(serialized)
    except (TypeError, ValueError, UnicodeError) as error:
        raise RuntimeError("Owned cron definition is not JSON-safe") from error
    payload = raw.get("payload")
    if isinstance(payload, dict) and payload.get("env") == {}:
        payload.pop("env")
    schedule = raw.get("schedule")
    delivery = raw.get("delivery")
    alert = raw.get("failureAlert")
    job_id = raw.get("id")
    name = raw.get("name")
    description = raw.get("description")
    declaration_key = raw.get("declarationKey")
    if not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id):
        raise RuntimeError("Owned cron definition id is not safely restorable")
    if not isinstance(name, str) or not name or name != name.strip() or name.startswith("-") \
            or len(name) > 512 or "\x00" in name:
        raise RuntimeError("Owned cron definition name is not safely restorable")
    if description is not None and (
        not isinstance(description, str) or not description or description != description.strip()
        or description.startswith("-")
        or len(description) > 4096
        or "\x00" in description
    ):
        raise RuntimeError("Owned cron definition description is not safely restorable")
    if not isinstance(declaration_key, str) or not declaration_key \
            or declaration_key != declaration_key.strip() \
            or declaration_key.startswith("-") \
            or len(declaration_key) > 512 or "\x00" in declaration_key:
        raise RuntimeError("Owned cron definition declaration key is not safely restorable")
    if type(raw.get("enabled")) is not bool:
        raise RuntimeError("Owned cron definition enabled state is not safely restorable")
    if raw.get("sessionTarget") != "isolated" or raw.get("sessionKey") is not None \
            or raw.get("agentId") is not None:
        raise RuntimeError("Owned cron definition session routing is not safely restorable")
    if type(raw.get("deleteAfterRun")) is not bool \
            or not _default_cron_behavior_contract(raw):
        raise RuntimeError("Owned cron definition behavior fields are not safely restorable")
    allowed_payload = {
        "kind", "argv", "cwd", "timeoutSeconds", "noOutputTimeoutSeconds", "outputMaxBytes",
        "env",
    }
    if isinstance(payload, dict) and "toolsAllow" in payload:
        raise RuntimeError("Owned command cron definition tools policy is not safely restorable")
    if not isinstance(payload, dict) or set(payload) not in (
        allowed_payload - {"env"}, allowed_payload,
    ):
        raise RuntimeError("Owned cron definition payload is outside the safe rollback allowlist")
    if payload.get("kind") != "command":
        raise RuntimeError("Owned cron definition payload is not a command")
    if not isinstance(schedule, dict):
        raise RuntimeError("Owned cron definition schedule is outside the safe rollback allowlist")
    kind = schedule.get("kind")
    if kind == "cron":
        schedule_valid = (
            set(schedule) == {"kind", "expr", "tz", "staggerMs"}
            and isinstance(schedule.get("expr"), str)
            and _valid_numeric_cron_expression(schedule["expr"])
            and isinstance(schedule.get("tz"), str) and bool(schedule["tz"])
            and schedule["tz"] == schedule["tz"].strip()
            and not schedule["tz"].startswith("-")
            and len(schedule["tz"]) <= 128 and "\x00" not in schedule["tz"]
            and type(schedule.get("staggerMs")) is int and schedule["staggerMs"] == 0
        )
        if schedule_valid:
            try:
                ZoneInfo(schedule["tz"])
            except (KeyError, ValueError):
                schedule_valid = False
    elif kind == "at":
        at = schedule.get("at")
        schedule_valid = set(schedule) == {"kind", "at"} and isinstance(at, str)
        if schedule_valid:
            try:
                parsed_at = datetime.fromisoformat(at.replace("Z", "+00:00"))
                canonical_at = parsed_at.astimezone(timezone.utc).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z")
                schedule_valid = (
                    parsed_at.tzinfo is not None and at == canonical_at
                    and not at.startswith("-")
                    and len(at) <= 128 and "\x00" not in at
                )
            except (ValueError, OverflowError):
                schedule_valid = False
    elif kind == "every":
        schedule_valid = (
            set(schedule) == {"kind", "everyMs"}
            and type(schedule.get("everyMs")) is int
            and 1 <= schedule["everyMs"] <= 31 * 24 * 60 * 60 * 1000
        )
    else:
        schedule_valid = False
    if not schedule_valid:
        raise RuntimeError("Owned cron definition schedule is not exactly restorable")
    if not isinstance(delivery, dict) or set(delivery) - {"mode", "channel", "to", "accountId"}:
        raise RuntimeError("Owned cron definition delivery is outside the safe rollback allowlist")
    if delivery.get("mode") == "none":
        delivery_valid = delivery == {"mode": "none"}
    elif delivery.get("mode") == "announce":
        delivery_valid = (
            set(delivery) in (
                {"mode", "channel", "to"},
                {"mode", "channel", "to", "accountId"},
            )
            and all(
                isinstance(delivery.get(key), str) and bool(delivery[key])
                and delivery[key] == delivery[key].strip()
                and not delivery[key].startswith("-")
                and len(delivery[key]) <= 512 and "\x00" not in delivery[key]
                for key in ("channel", "to")
            )
            and delivery["channel"] == delivery["channel"].lower()
            and (
                "accountId" not in delivery
                or isinstance(delivery["accountId"], str)
                and SAFE_ACCOUNT_ID_RE.fullmatch(delivery["accountId"]) is not None
            )
        )
    else:
        delivery_valid = False
    if not delivery_valid:
        raise RuntimeError("Owned cron definition delivery is not exactly restorable")
    if alert is not None and (
        not isinstance(alert, dict)
        or set(alert) - {"after", "cooldownMs", "includeSkipped", "mode", "channel", "to", "accountId"}
    ):
        raise RuntimeError("Owned cron definition alert is outside the safe rollback allowlist")
    if alert is not None:
        required_alert = {"after", "cooldownMs", "includeSkipped", "channel"}
        if not required_alert.issubset(alert) \
                or type(alert.get("after")) is not int or not 1 <= alert["after"] <= 1_000_000 \
                or type(alert.get("cooldownMs")) is not int \
                or not 0 <= alert["cooldownMs"] <= 31 * 24 * 60 * 60 * 1000 \
                or type(alert.get("includeSkipped")) is not bool \
                or not isinstance(alert.get("channel"), str) or not alert["channel"] \
                or alert["channel"] != alert["channel"].strip().lower() \
                or alert["channel"].startswith("-") \
                or len(alert["channel"]) > 512 or "\x00" in alert["channel"] \
                or "mode" in alert and alert["mode"] != "announce" \
                or "to" in alert and (
                    not isinstance(alert["to"], str) or not alert["to"]
                    or alert["to"] != alert["to"].strip()
                    or alert["to"].startswith("-")
                    or len(alert["to"]) > 512 or "\x00" in alert["to"]
                ) \
                or "accountId" in alert and (
                    not isinstance(alert["accountId"], str)
                    or SAFE_ACCOUNT_ID_RE.fullmatch(alert["accountId"]) is None
                ):
            raise RuntimeError("Owned cron definition alert is not exactly restorable")
    argv = payload.get("argv")
    env = payload.get("env", {})
    if not isinstance(argv, list) or not 1 <= len(argv) <= 16 \
            or any(
                not isinstance(item, str) or not item or len(item) > 8192 or "\x00" in item
                for item in argv
            ):
        raise RuntimeError("Owned cron definition argv is outside the safe rollback allowlist")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd or cwd != cwd.strip() \
            or len(cwd) > 4096 or "\x00" in cwd \
            or not Path(cwd).is_absolute():
        raise RuntimeError("Owned cron definition cwd is outside the safe rollback allowlist")
    for field, maximum in (
        ("timeoutSeconds", 31 * 24 * 60 * 60),
        ("noOutputTimeoutSeconds", 31 * 24 * 60 * 60),
        ("outputMaxBytes", 64 * 1024 * 1024),
    ):
        value = payload.get(field)
        if type(value) is not int or not 1 <= value <= maximum:
            raise RuntimeError("Owned cron definition resource limit is not safely restorable")
    if not isinstance(env, dict) or len(env) > 16 \
            or any(not isinstance(key, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) is None
                   or not isinstance(value, str)
                   or len(value) > 4096 or "\x00" in value
                   or _sensitive_key(key) or _contains_forbidden_key(value)
                   for key, value in env.items()):
        raise RuntimeError("Owned cron definition environment is outside the safe rollback allowlist")
    if _contains_forbidden_key(raw):
        raise RuntimeError("Owned cron definition contains sensitive material")
    return {key: value for key, value in raw.items() if value is not None}


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
                 node_path: Path, agent: str = "main", launchctl: str | Path = "/bin/launchctl",
                 snapshot_root: Path | None = None, report_channel: str = "",
                 report_to: str | None = None, timezone_name: str = "Asia/Taipei",
                 legacy_snapshot_job_id: str | None = None,
                 legacy_snapshot_job_sha256: str | None = None,
                 approved_disabled_collision: ApprovedDisabledCronCollision | None = None,
                 report_account_id: str = "default",
                 python_path: Path | None = None) -> None:
        self.paths = paths
        self.repo_root = Path(repo_root).resolve()
        self.cli = cli
        self.node_path = Path(node_path).resolve()
        self.agent = agent
        self.launchctl = str(launchctl)
        self.snapshot_root = Path(os.path.abspath(
            snapshot_root if snapshot_root is not None else paths.state_root / "knowledge-snapshots"
        ))
        self.report_channel = report_channel
        self.report_to = report_to
        self.timezone_name = timezone_name
        self.legacy_snapshot_job_id = legacy_snapshot_job_id
        self.legacy_snapshot_job_sha256 = legacy_snapshot_job_sha256
        if approved_disabled_collision is not None and not isinstance(
            approved_disabled_collision, ApprovedDisabledCronCollision,
        ):
            raise TypeError("Approved disabled collision must use the closed approval contract")
        self.approved_disabled_collision = approved_disabled_collision
        self.report_account_id = report_account_id
        self.python_path = Path(python_path or sys.executable).resolve()
        self.store = TransactionStore(paths.state_root)
        self.plugin_source = self.repo_root / "plugin" / PLUGIN_ID
        self.skill_source = self.repo_root / "openclaw-lancedb-knowledge-local"
        self.plugin_target = self.paths.home / ".openclaw" / "extensions" / PLUGIN_ID

    def preflight(self) -> dict[str, Any]:
        self.paths.validate()
        for file_path in (self.node_path, self.python_path, Path(self.cli.executable)):
            if file_path.is_symlink() or not file_path.is_file() or not os.access(file_path, os.X_OK):
                raise RuntimeError("Required executable is missing or unsafe")
        for directory in (self.plugin_source, self.skill_source):
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("Integration source package is missing or unsafe")
        _assert_no_symlink_components(self.plugin_target)
        _assert_specific_child(self.plugin_target, self.paths.home, "plugin target")
        if self.plugin_target.exists() and not self.plugin_target.is_dir():
            raise RuntimeError("Existing OpenClaw plugin target is unsafe")
        try:
            ZoneInfo(self.timezone_name)
        except Exception as error:
            raise RuntimeError("Integration timezone is not a valid IANA timezone") from error
        if self.report_channel != "discord":
            raise RuntimeError("Failure-alert channel must be the explicit Discord provider")
        if self.report_to is None or not DISCORD_CHANNEL_TARGET_RE.fullmatch(self.report_to):
            raise RuntimeError("Failure-alert destination must be an explicit Discord channel target")
        if not SAFE_ACCOUNT_ID_RE.fullmatch(self.report_account_id):
            raise RuntimeError("Failure-alert account identity is invalid")
        if bool(self.legacy_snapshot_job_id) != bool(self.legacy_snapshot_job_sha256):
            raise RuntimeError("Operator-selected legacy migration requires both job id and SHA-256 fingerprint")
        if self.legacy_snapshot_job_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", self.legacy_snapshot_job_sha256
        ):
            raise RuntimeError("Operator-selected legacy snapshot fingerprint is invalid")
        _assert_no_symlink_components(self.snapshot_root)
        _assert_specific_child(self.snapshot_root, self.paths.home, "snapshot root")
        project = self.paths.project_root.resolve(strict=False)
        snapshot = self.snapshot_root.resolve(strict=False)
        if snapshot == project or project in snapshot.parents:
            raise RuntimeError("Snapshot root must not be inside the live Qwen project")
        if self.snapshot_root.exists():
            metadata = self.snapshot_root.stat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise RuntimeError("Snapshot root ownership or permissions are unsafe")
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

    def _remove_tree_at(self, parent_fd: int, name: str, expected_identity: tuple[int, int],
                        *, expected_root_marker_sha256: str | None = None) -> None:
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            metadata = os.fstat(directory_fd)
            self._validate_private_directory(metadata)
            if (metadata.st_dev, metadata.st_ino) != expected_identity:
                raise RuntimeError("Snapshot run changed before cleanup")
            if expected_root_marker_sha256 is not None:
                self._verify_snapshot_marker(directory_fd, expected_root_marker_sha256)
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
            self._remove_tree_at(
                snapshot_fd, run_dir.name, expected_identity,
                expected_root_marker_sha256=expected_marker_sha256,
            )

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

    @staticmethod
    def _safe_plugin_symlink(root: Path, path: Path, *, label: str) -> str:
        relative = path.relative_to(root)
        if relative != Path("node_modules/openclaw"):
            raise RuntimeError(f"{label} contains an unsupported symbolic link")
        try:
            target = os.readlink(path)
        except OSError as error:
            raise RuntimeError(f"{label} symbolic link could not be read safely") from error
        if not target or len(target) > 4096 or "\x00" in target:
            raise RuntimeError(f"{label} contains an unsafe symbolic link target")
        target_path = Path(target)
        if not target_path.is_absolute() or ".." in target_path.parts \
                or target_path.parts[-3:] != ("lib", "node_modules", "openclaw"):
            raise RuntimeError(f"{label} contains an unsafe symbolic link target")
        try:
            target_meta = target_path.lstat()
        except OSError as error:
            raise RuntimeError(f"{label} symbolic link destination is missing or unsafe") from error
        if target_path.is_symlink() or not stat.S_ISDIR(target_meta.st_mode) \
                or target_meta.st_uid != os.getuid() \
                or target_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(f"{label} symbolic link destination is unsafe")
        return target

    @staticmethod
    def _asset_symlink_policy(relative: str, target: str) -> None:
        if relative != "node_modules/openclaw":
            raise RuntimeError("Rollback asset contains an unsupported symbolic link")
        if not target or len(target) > 4096 or "\x00" in target:
            raise RuntimeError("Rollback asset contains an unsafe symbolic link target")
        target_path = Path(target)
        if not target_path.is_absolute() or ".." in target_path.parts \
                or target_path.parts[-3:] != ("lib", "node_modules", "openclaw"):
            raise RuntimeError("Rollback asset contains an unsafe symbolic link target")
        try:
            target_meta = target_path.lstat()
        except OSError as error:
            raise RuntimeError("Rollback asset symbolic link destination is missing") from error
        if target_path.is_symlink() or not stat.S_ISDIR(target_meta.st_mode) \
                or target_meta.st_uid != os.getuid() \
                or target_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("Rollback asset symbolic link destination is unsafe")

    @staticmethod
    def _skill_asset_symlink_policy(relative: str, target: str) -> None:
        expected_relative = (
            "assets/knowledge-lancedb-template/node_modules/.bin/arrow2csv"
        )
        expected_target = "../apache-arrow/bin/arrow2csv.js"
        if relative != expected_relative:
            raise RuntimeError("Rollback asset contains an unsupported symbolic link")
        if target != expected_target:
            raise RuntimeError("Rollback asset contains an unsafe symbolic link target")

    def _safe_tree_sha256(
        self,
        root: Path,
        *,
        label: str,
        symlink_policy: Callable[[str, str], None] | None = None,
    ) -> str:
        try:
            with self._open_private_directory(root.parent) as parent_fd:
                return inspect_asset_at(
                    parent_fd,
                    root.name,
                    kind="directory",
                    symlink_policy=symlink_policy,
                ).sha256
        except RuntimeError:
            raise
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{label} is missing or unsafe") from error

    def _copy_safe_tree(
        self,
        source: Path,
        target: Path,
        *,
        label: str,
        symlink_policy: Callable[[str, str], None] | None = None,
    ) -> str:
        expected = self._safe_tree_sha256(
            source, label=label, symlink_policy=symlink_policy,
        )
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"{label} backup target already exists")
        shutil.copytree(source, target, symlinks=True)
        if self._safe_tree_sha256(
            target,
            label=f"{label} backup",
            symlink_policy=symlink_policy,
        ) != expected:
            raise RuntimeError(f"{label} backup verification failed")
        return expected

    def _rollback_asset_specs(self) -> tuple[RollbackAssetSpec, ...]:
        project_backup = Path("project-runtime.preinstall")
        return (
            RollbackAssetSpec(
                "plugin", self.plugin_target, Path("plugin.preinstall"),
                "directory", "pluginMutationStarted",
            ),
            RollbackAssetSpec(
                "skill", self.paths.workspace / "skills" / SKILL_ID,
                Path("skill.preinstall"), "directory", "skillMutationStarted",
            ),
            RollbackAssetSpec(
                "project.src", self.paths.project_root / "src",
                project_backup / "src", "directory", "projectRuntimeMutationStarted",
            ),
            RollbackAssetSpec(
                "project.scripts", self.paths.project_root / "scripts",
                project_backup / "scripts", "directory", "projectRuntimeMutationStarted",
            ),
            RollbackAssetSpec(
                "project.package_json", self.paths.project_root / "package.json",
                project_backup / "package.json", "file", "projectRuntimeMutationStarted",
            ),
            RollbackAssetSpec(
                "project.package_lock", self.paths.project_root / "package-lock.json",
                project_backup / "package-lock.json", "file", "projectRuntimeMutationStarted",
            ),
        )

    def _safe_file_identity(self, path: Path, *, label: str) -> dict[str, Any]:
        try:
            with self._open_private_directory(path.parent) as parent_fd:
                return inspect_asset_at(parent_fd, path.name, kind="file").as_dict()
        except RuntimeError:
            raise
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{label} is missing or unsafe") from error

    def _safe_directory_identity(
        self,
        path: Path,
        *,
        label: str,
        symlink_policy: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        try:
            with self._open_private_directory(path.parent) as parent_fd:
                return inspect_asset_at(
                    parent_fd,
                    path.name,
                    kind="directory",
                    symlink_policy=symlink_policy,
                ).as_dict()
        except RuntimeError:
            raise
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{label} is missing or unsafe") from error

    def _safe_asset_identity(
        self,
        path: Path,
        *,
        kind: str,
        label: str,
        symlink_policy: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        if kind == "directory":
            return self._safe_directory_identity(
                path, label=label, symlink_policy=symlink_policy,
            )
        if kind == "file":
            return self._safe_file_identity(path, label=label)
        raise RuntimeError("Rollback asset kind is unsupported")

    def _copy_safe_file(self, source: Path, target: Path, *, label: str) -> str:
        source_identity = self._safe_file_identity(source, label=label)
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"{label} backup target already exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source_fd: int | None = None
        target_fd: int | None = None
        try:
            with self._open_private_directory(source.parent) as source_parent_fd, \
                    self._open_private_directory(target.parent) as target_parent_fd:
                source_fd = os.open(
                    source.name,
                    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                    dir_fd=source_parent_fd,
                )
                target_fd = os.open(
                    target.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    int(source_identity["mode"]),
                    dir_fd=target_parent_fd,
                )
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
                    os.fchmod(output_handle.fileno(), int(source_identity["mode"]))
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                os.fsync(target_parent_fd)
        except OSError as error:
            raise RuntimeError(f"{label} backup copy failed safely") from error
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if target_fd is not None:
                os.close(target_fd)
        copied = self._safe_file_identity(target, label=f"{label} backup")
        if copied["sha256"] != source_identity["sha256"]:
            raise RuntimeError(f"{label} backup verification failed")
        return str(source_identity["sha256"])

    def _snapshot_other_assets(self, snapshot_dir: Path) -> dict[str, Any]:
        plugin_target = self.plugin_target
        plugin_backup = snapshot_dir / "plugin.preinstall"
        skill_target = self.paths.workspace / "skills" / SKILL_ID
        skill_backup = snapshot_dir / "skill.preinstall"
        plist_backup = snapshot_dir / "launchd.preinstall.plist"
        project_backup = snapshot_dir / "project-runtime.preinstall"
        health_receipt = self.paths.project_root / "reports/backup-health-component.qwen-local.json"
        health_receipt_backup = snapshot_dir / "health-receipt.preinstall.json"
        managed_backups = (
            plugin_backup, skill_backup, plist_backup, project_backup, health_receipt_backup,
        )
        if any(candidate.exists() or candidate.is_symlink() for candidate in managed_backups):
            raise RuntimeError("A non-config preinstall snapshot already exists")
        plugin_existed = plugin_target.exists()
        skill_existed = skill_target.exists()
        plist_existed = self.paths.launchd_plist.exists()
        project_existed = self.paths.project_root.exists()
        health_receipt_existed = health_receipt.exists()
        asset_receipts: dict[str, dict[str, Any]] = {}
        if project_existed:
            if self.paths.project_root.is_symlink() or not self.paths.project_root.is_dir():
                raise RuntimeError("Existing Qwen project root is unsafe")
            project_backup.mkdir(mode=0o700)
        for spec in self._rollback_asset_specs():
            target = spec.target
            backup = snapshot_dir / spec.backup_relative
            symlink_policy = self._asset_symlink_policy_for(spec)
            if target.is_symlink():
                raise RuntimeError(f"Existing rollback asset {spec.asset_id} is a symbolic link")
            existed = target.exists()
            parent_existed = target.parent.exists()
            parent_fields: dict[str, Any]
            if parent_existed:
                parent_meta = target.parent.lstat()
                self._validate_private_directory(parent_meta)
                parent_fields = {
                    "canonicalParent": str(Path(os.path.abspath(target.parent))),
                    "canonicalName": target.name,
                    "parentDev": parent_meta.st_dev,
                    "parentIno": parent_meta.st_ino,
                }
            elif not existed:
                parent_fields = {
                    "canonicalParent": str(Path(os.path.abspath(target.parent))),
                    "canonicalName": target.name,
                    "parentDev": None,
                    "parentIno": None,
                }
            else:
                raise RuntimeError(f"Rollback asset parent for {spec.asset_id} is missing")
            receipt: dict[str, Any] = {
                **parent_fields,
                "parentPreExisted": parent_existed,
                "parentCreatePlanned": not parent_existed,
                "parentCreated": False,
                "parentPublished": False,
                "preExisted": existed,
                "preKind": spec.kind if existed else None,
                "preDev": None,
                "preIno": None,
                "preMode": None,
                "preSha256": None,
                "backupRelativeName": spec.backup_relative.as_posix(),
                "backupKind": spec.kind if existed else None,
                "backupDev": None,
                "backupIno": None,
                "backupMode": None,
                "backupSha256": None,
                "mutationField": spec.mutation_field,
                "mutationStarted": False,
            }
            if existed:
                before = self._safe_asset_identity(
                    target,
                    kind=spec.kind,
                    label=f"Existing rollback asset {spec.asset_id}",
                    symlink_policy=symlink_policy,
                )
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if spec.kind == "directory":
                    copied_sha256 = self._copy_safe_tree(
                        target,
                        backup,
                        label=f"Existing rollback asset {spec.asset_id}",
                        symlink_policy=symlink_policy,
                    )
                else:
                    copied_sha256 = self._copy_safe_file(
                        target, backup, label=f"Existing rollback asset {spec.asset_id}",
                    )
                after = self._safe_asset_identity(
                    target,
                    kind=spec.kind,
                    label=f"Existing rollback asset {spec.asset_id}",
                    symlink_policy=symlink_policy,
                )
                if before != after or copied_sha256 != before["sha256"]:
                    raise RuntimeError(f"Rollback asset {spec.asset_id} changed during snapshot")
                backup_identity = self._safe_asset_identity(
                    backup,
                    kind=spec.kind,
                    label=f"Rollback backup {spec.asset_id}",
                    symlink_policy=symlink_policy,
                )
                if backup_identity["sha256"] != before["sha256"]:
                    raise RuntimeError(f"Rollback backup {spec.asset_id} failed verification")
                receipt.update({
                    "preKind": before["kind"],
                    "preDev": before["dev"],
                    "preIno": before["ino"],
                    "preMode": before["mode"],
                    "preSha256": before["sha256"],
                    "backupKind": backup_identity["kind"],
                    "backupDev": backup_identity["dev"],
                    "backupIno": backup_identity["ino"],
                    "backupMode": backup_identity["mode"],
                    "backupSha256": backup_identity["sha256"],
                })
            asset_receipts[spec.asset_id] = receipt
        if self.paths.launchd_plist.is_symlink():
            raise RuntimeError("Existing launchd plist is a symbolic link")
        if plist_existed:
            shutil.copy2(self.paths.launchd_plist, plist_backup)
            os.chmod(plist_backup, 0o600)
        if health_receipt.is_symlink():
            raise RuntimeError("Existing Qwen health receipt is a symbolic link")
        if health_receipt_existed:
            if not health_receipt.is_file():
                raise RuntimeError("Existing Qwen health receipt is unsafe")
            shutil.copy2(health_receipt, health_receipt_backup)
            os.chmod(health_receipt_backup, 0o600)
        return {
            "pluginTargetPath": str(plugin_target), "pluginBackupPath": str(plugin_backup),
            "pluginExisted": plugin_existed,
            "pluginBackupSha256": asset_receipts["plugin"]["backupSha256"],
            "skillTargetPath": str(skill_target), "skillBackupPath": str(skill_backup),
            "skillExisted": skill_existed,
            "skillBackupSha256": asset_receipts["skill"]["backupSha256"],
            "plistBackupPath": str(plist_backup), "plistExisted": plist_existed,
            "projectExisted": project_existed, "projectBackupPath": str(project_backup),
            "healthReceiptPath": str(health_receipt),
            "healthReceiptBackupPath": str(health_receipt_backup),
            "healthReceiptExisted": health_receipt_existed,
            "assetRecoverySchema": ASSET_ROLLBACK_RECEIPT_SCHEMA,
            "assetReceipts": asset_receipts,
        }

    @staticmethod
    def _receipt_identity(
        receipt: dict[str, Any], prefix: str, *, required: bool = False,
    ) -> AssetIdentity | None:
        values = {
            "kind": receipt.get(f"{prefix}Kind"),
            "dev": receipt.get(f"{prefix}Dev"),
            "ino": receipt.get(f"{prefix}Ino"),
            "mode": receipt.get(f"{prefix}Mode"),
            "sha256": receipt.get(f"{prefix}Sha256"),
        }
        if all(value is None for value in values.values()):
            if required:
                raise RuntimeError(f"Rollback asset {prefix} identity is missing")
            return None
        if values["kind"] not in {"directory", "file"} \
                or type(values["dev"]) is not int or type(values["ino"]) is not int \
                or type(values["mode"]) is not int or not 0 <= values["mode"] <= 0o7777 \
                or not isinstance(values["sha256"], str) \
                or re.fullmatch(r"[0-9a-f]{64}", values["sha256"]) is None:
            raise RuntimeError(f"Rollback asset {prefix} identity is malformed")
        return AssetIdentity(
            kind=str(values["kind"]),
            dev=int(values["dev"]),
            ino=int(values["ino"]),
            mode=int(values["mode"]),
            sha256=str(values["sha256"]),
        )

    @staticmethod
    def _identity_fields(prefix: str, identity: AssetIdentity) -> dict[str, Any]:
        return {
            f"{prefix}Kind": identity.kind,
            f"{prefix}Dev": identity.dev,
            f"{prefix}Ino": identity.ino,
            f"{prefix}Mode": identity.mode,
            f"{prefix}Sha256": identity.sha256,
        }

    def _asset_symlink_policy_for(
        self, spec: RollbackAssetSpec,
    ) -> Callable[[str, str], None] | None:
        if spec.asset_id == "plugin":
            return self._asset_symlink_policy
        if spec.asset_id == "skill":
            return self._skill_asset_symlink_policy
        return None

    def _inspect_optional_asset(
        self, spec: RollbackAssetSpec, *, name: str | None = None,
    ) -> AssetIdentity | None:
        if not os.path.lexists(spec.target.parent):
            return None
        with self._open_private_directory(spec.target.parent) as parent_fd:
            try:
                return inspect_asset_at(
                    parent_fd,
                    name or spec.target.name,
                    kind=spec.kind,
                    symlink_policy=self._asset_symlink_policy_for(spec),
                )
            except FileNotFoundError:
                return None

    def _checkpoint_asset_receipt(
        self, transaction: dict[str, Any], asset_id: str, updates: dict[str, Any],
    ) -> None:
        receipts = transaction.get("assetReceipts")
        if not isinstance(receipts, dict) or not isinstance(receipts.get(asset_id), dict):
            raise RuntimeError("Rollback asset receipt is missing")
        receipt = receipts[asset_id]
        validated: dict[str, Any] = {}
        for key, value in updates.items():
            current = receipt.get(key)
            if current == value:
                continue
            if isinstance(value, bool):
                if current not in {None, False} or value is not True:
                    raise RuntimeError("Rollback asset state is not monotonic")
            elif current is not None:
                raise RuntimeError("Rollback asset identity attempted to change")
            validated[key] = value
        receipt.update(validated)
        self.store.write(transaction)

    @staticmethod
    def _asset_parent_identity(
        spec: RollbackAssetSpec, receipt: dict[str, Any], *, require_owned: bool,
    ) -> tuple[int, int] | None:
        parent_dev = receipt.get("parentDev")
        parent_ino = receipt.get("parentIno")
        if (parent_dev is None) != (parent_ino is None) or parent_dev is not None and (
            type(parent_dev) is not int or type(parent_ino) is not int
        ):
            raise RuntimeError(f"Rollback asset parent receipt for {spec.asset_id} is malformed")
        pre_existed = receipt.get("parentPreExisted", parent_dev is not None)
        create_planned = receipt.get("parentCreatePlanned", False)
        created = receipt.get("parentCreated", False)
        published = receipt.get("parentPublished", False)
        if any(type(value) is not bool for value in (
            pre_existed, create_planned, created, published,
        )):
            raise RuntimeError(f"Rollback asset parent receipt for {spec.asset_id} is malformed")
        if pre_existed:
            if parent_dev is None or create_planned or created or published:
                raise RuntimeError(f"Rollback asset parent receipt for {spec.asset_id} is malformed")
            return parent_dev, parent_ino
        if not create_planned or created is not published:
            raise RuntimeError(f"Rollback asset parent receipt for {spec.asset_id} is malformed")
        if created:
            if parent_dev is None:
                raise RuntimeError(f"Rollback asset parent receipt for {spec.asset_id} is malformed")
            return parent_dev, parent_ino
        if parent_dev is not None or require_owned:
            raise RuntimeError(f"Rollback asset parent transition for {spec.asset_id} is not checkpointed")
        return None

    def _assert_asset_parent_identity(
        self,
        parent_fd: int,
        spec: RollbackAssetSpec,
        receipt: dict[str, Any],
    ) -> None:
        expected = self._asset_parent_identity(spec, receipt, require_owned=True)
        assert expected is not None
        opened = os.fstat(parent_fd)
        if (opened.st_dev, opened.st_ino) != expected:
            raise RuntimeError(f"Rollback asset parent for {spec.asset_id} changed")
        try:
            public = os.stat(spec.target.parent, follow_symlinks=False)
        except OSError as error:
            raise RuntimeError(f"Rollback asset parent for {spec.asset_id} changed") from error
        if not stat.S_ISDIR(public.st_mode) or (public.st_dev, public.st_ino) != expected:
            raise RuntimeError(f"Rollback asset parent for {spec.asset_id} changed")

    @contextmanager
    def _open_checked_asset_parent(
        self, spec: RollbackAssetSpec, receipt: dict[str, Any],
    ) -> Iterator[int]:
        with self._open_private_directory(spec.target.parent) as parent_fd:
            self._assert_asset_parent_identity(parent_fd, spec, receipt)
            try:
                yield parent_fd
            except BaseException:
                raise
            else:
                self._assert_asset_parent_identity(parent_fd, spec, receipt)

    def _checkpoint_fresh_asset_parent(
        self, transaction: dict[str, Any], spec: RollbackAssetSpec,
        receipt: dict[str, Any], *, create_if_missing: bool = True,
    ) -> None:
        if self._asset_parent_identity(spec, receipt, require_owned=False) is not None:
            return
        if spec.asset_id.startswith("project."):
            project_identity = (
                transaction.get("projectRootDev"), transaction.get("projectRootIno"),
            )
            if transaction.get("projectCreated") is not True:
                if not create_if_missing and not os.path.lexists(spec.target.parent):
                    return
                raise RuntimeError("Project runtime parent transition is not checkpointed")
            if any(type(value) is not int for value in project_identity) \
                    or spec.target.parent != self.paths.project_root:
                raise RuntimeError("Project runtime parent transition is not checkpointed")
            with self._open_private_directory(spec.target.parent) as parent_fd:
                current = os.fstat(parent_fd)
                if (current.st_dev, current.st_ino) != project_identity:
                    raise RuntimeError(f"Rollback asset parent for {spec.asset_id} changed")
            self._checkpoint_asset_receipt(transaction, spec.asset_id, {
                "parentCreated": True,
                "parentPublished": True,
                "parentDev": project_identity[0],
                "parentIno": project_identity[1],
            })
            return
        if spec.asset_id not in {"plugin", "skill"}:
            raise RuntimeError("Rollback asset parent transition is unsupported")

        stage_name = receipt.get("parentStageName")
        stage_dev = receipt.get("parentStageDev")
        stage_ino = receipt.get("parentStageIno")
        if stage_name is None and stage_dev is None and stage_ino is None:
            if not create_if_missing:
                if os.path.lexists(spec.target.parent):
                    raise RuntimeError(
                        f"Rollback asset parent for {spec.asset_id} appeared without ownership"
                    )
                return
            stage_name = f".qwen-asset-parent-install-{uuid.uuid4().hex}"
            with self._open_private_directory(spec.target.parent.parent) as ancestor_fd:
                try:
                    os.stat(spec.target.parent.name, dir_fd=ancestor_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError(
                        f"Rollback asset parent for {spec.asset_id} appeared before creation"
                    )
                os.mkdir(stage_name, mode=0o700, dir_fd=ancestor_fd)
                stage_fd = os.open(
                    stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=ancestor_fd,
                )
                try:
                    metadata = os.fstat(stage_fd)
                    self._validate_restricted_directory(metadata)
                    os.fsync(stage_fd)
                    os.fsync(ancestor_fd)
                finally:
                    os.close(stage_fd)
            stage_dev, stage_ino = metadata.st_dev, metadata.st_ino
            self._checkpoint_asset_receipt(transaction, spec.asset_id, {
                "parentStageName": stage_name,
                "parentStageDev": stage_dev,
                "parentStageIno": stage_ino,
            })
        elif not isinstance(stage_name, str) \
                or ASSET_PARENT_STAGE_RE.fullmatch(stage_name) is None \
                or type(stage_dev) is not int or type(stage_ino) is not int:
            raise RuntimeError(f"Rollback asset parent transition for {spec.asset_id} is malformed")

        expected = (stage_dev, stage_ino)
        with self._open_private_directory(spec.target.parent.parent) as ancestor_fd:
            def identity(name: str) -> tuple[int, int] | None:
                try:
                    metadata = os.stat(name, dir_fd=ancestor_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                self._validate_restricted_directory(metadata)
                return metadata.st_dev, metadata.st_ino

            stage_current = identity(stage_name)
            final_current = identity(spec.target.parent.name)
            if stage_current is not None and final_current is not None:
                raise RuntimeError(f"Rollback asset parent transition for {spec.asset_id} is ambiguous")
            if final_current is not None:
                if final_current != expected:
                    raise RuntimeError(f"Rollback asset parent for {spec.asset_id} changed")
            else:
                if stage_current != expected:
                    raise RuntimeError(f"Rollback asset parent stage for {spec.asset_id} changed")
                _rename_noreplace_at(
                    ancestor_fd, stage_name, ancestor_fd, spec.target.parent.name,
                )
                os.fsync(ancestor_fd)
                final_current = identity(spec.target.parent.name)
                if final_current != expected:
                    raise RuntimeError(f"Rollback asset parent for {spec.asset_id} changed")
        self._checkpoint_asset_receipt(transaction, spec.asset_id, {
            "parentCreated": True,
            "parentPublished": True,
            "parentDev": expected[0],
            "parentIno": expected[1],
        })

    def _checkpoint_asset_mutation(
        self, transaction: dict[str, Any] | None, asset_ids: Iterable[str],
    ) -> None:
        if transaction is None:
            return
        specs = {spec.asset_id: spec for spec in self._rollback_asset_specs()}
        receipts = transaction.get("assetReceipts")
        if transaction.get("assetRecoverySchema") != ASSET_ROLLBACK_RECEIPT_SCHEMA \
                or not isinstance(receipts, dict):
            raise RuntimeError("Rollback asset receipt set is missing")
        changed = False
        for asset_id in asset_ids:
            spec = specs.get(asset_id)
            receipt = receipts.get(asset_id)
            if spec is None or not isinstance(receipt, dict):
                raise RuntimeError("Rollback asset receipt is missing")
            self._checkpoint_fresh_asset_parent(transaction, spec, receipt)
            with self._open_private_directory(spec.target.parent) as parent_fd:
                self._assert_asset_parent_identity(parent_fd, spec, receipt)
            if receipt.get("mutationStarted") is not True:
                if receipt.get("mutationStarted") is not False:
                    raise RuntimeError("Rollback asset mutation receipt is malformed")
                receipt["mutationStarted"] = True
                changed = True
            if transaction.get(spec.mutation_field) is not True:
                if transaction.get(spec.mutation_field) not in {None, False}:
                    raise RuntimeError("Rollback asset mutation state is malformed")
                transaction[spec.mutation_field] = True
                changed = True
        if changed:
            self.store.write(transaction)

    def _capture_asset_post_identity(
        self, transaction: dict[str, Any] | None, asset_id: str,
    ) -> None:
        if transaction is None:
            return
        specs = {spec.asset_id: spec for spec in self._rollback_asset_specs()}
        spec = specs.get(asset_id)
        if spec is None:
            raise RuntimeError("Rollback asset identity is unsupported")
        if not os.path.lexists(spec.target.parent):
            raise RuntimeError("Installed rollback asset parent is missing")
        receipts = transaction.get("assetReceipts")
        if not isinstance(receipts, dict) or not isinstance(receipts.get(asset_id), dict):
            raise RuntimeError("Rollback asset receipt is missing")
        receipt = receipts[asset_id]
        with self._open_checked_asset_parent(spec, receipt) as parent_fd:
            parent = os.fstat(parent_fd)
            post = inspect_asset_at(
                parent_fd,
                spec.target.name,
                kind=spec.kind,
                symlink_policy=self._asset_symlink_policy_for(spec),
            )
        updates = {
            "postParentDev": parent.st_dev,
            "postParentIno": parent.st_ino,
            **self._identity_fields("post", post),
        }
        self._checkpoint_asset_receipt(transaction, asset_id, updates)

    @staticmethod
    def _inspect_optional_asset_at(
        parent_fd: int,
        name: str,
        *,
        kind: str,
        symlink_policy: Callable[[str, str], None] | None,
    ) -> AssetIdentity | None:
        try:
            return inspect_asset_at(
                parent_fd,
                name,
                kind=kind,
                symlink_policy=symlink_policy,
            )
        except FileNotFoundError:
            return None

    def _project_runtime_source_identity(
        self,
        source: Path,
        spec: RollbackAssetSpec,
    ) -> AssetIdentity:
        if source.is_symlink() or not source.exists():
            raise RuntimeError("Qwen project runtime source is missing or unsafe")
        try:
            with self._open_private_directory(source.parent) as source_parent_fd:
                return inspect_asset_at(
                    source_parent_fd,
                    source.name,
                    kind=spec.kind,
                    symlink_policy=None,
                )
        except RuntimeError:
            raise
        except (OSError, ValueError) as error:
            raise RuntimeError("Qwen project runtime source is missing or unsafe") from error

    def _synchronize_one_project_asset(
        self,
        transaction: dict[str, Any],
        spec: RollbackAssetSpec,
        source: Path,
    ) -> None:
        receipts = transaction.get("assetReceipts")
        if transaction.get("assetRecoverySchema") != ASSET_ROLLBACK_RECEIPT_SCHEMA \
                or not isinstance(receipts, dict) \
                or not isinstance(receipts.get(spec.asset_id), dict):
            raise RuntimeError("Project runtime synchronization receipt is missing")
        receipt = receipts[spec.asset_id]
        if receipt.get("mutationStarted") is not True:
            raise RuntimeError("Project runtime synchronization was not checkpointed")
        policy = self._asset_symlink_policy_for(spec)
        if policy is not None:
            raise RuntimeError("Project runtime synchronization policy is unsafe")

        source_current = self._project_runtime_source_identity(source, spec)
        source_expected = self._receipt_identity(receipt, "installSource", required=False)
        if source_expected is None:
            self._checkpoint_asset_receipt(
                transaction,
                spec.asset_id,
                self._identity_fields("installSource", source_current),
            )
            source_expected = source_current
        elif source_current != source_expected:
            raise RuntimeError("Qwen project runtime source changed")

        pre = self._receipt_identity(
            receipt, "pre", required=receipt.get("preExisted") is True,
        )
        if receipt.get("preExisted") is False and pre is not None:
            raise RuntimeError("Project runtime absence receipt is inconsistent")
        if pre is not None and pre.kind != spec.kind:
            raise RuntimeError("Project runtime preinstall identity is inconsistent")

        stage_name = receipt.get("installStageName")
        stage = self._receipt_identity(receipt, "installStage", required=False)
        quarantine_name = receipt.get("installQuarantineName")
        if stage_name is not None and (
            not isinstance(stage_name, str)
            or ASSET_INSTALL_STAGE_RE.fullmatch(stage_name) is None
            or stage is None
        ):
            raise RuntimeError("Project runtime install stage receipt is malformed")
        if stage is not None and (
            not isinstance(stage_name, str) or stage.kind != spec.kind
        ):
            raise RuntimeError("Project runtime install stage receipt is malformed")
        if quarantine_name is not None and (
            not isinstance(quarantine_name, str)
            or QUARANTINE_RE.fullmatch(quarantine_name) is None
        ):
            raise RuntimeError("Project runtime quarantine receipt is malformed")

        with self._open_private_directory(spec.target.parent) as target_parent_fd:
            self._assert_asset_parent_identity(target_parent_fd, spec, receipt)
            if receipt.get("installComplete") is True:
                if stage is None:
                    raise RuntimeError("Completed project runtime receipt is missing its identity")
                current = self._inspect_optional_asset_at(
                    target_parent_fd,
                    spec.target.name,
                    kind=spec.kind,
                    symlink_policy=policy,
                )
                post = self._receipt_identity(receipt, "post", required=True)
                if current != post or post != stage or source_current.sha256 != stage.sha256:
                    raise RuntimeError("Completed project runtime asset changed")
                if isinstance(quarantine_name, str) and self._inspect_optional_asset_at(
                    target_parent_fd,
                    quarantine_name,
                    kind=spec.kind,
                    symlink_policy=policy,
                ) is not None:
                    raise RuntimeError("Completed project runtime quarantine still exists")
                return

            if stage is None:
                stage_name = f".qwen-asset-install-{uuid.uuid4().hex}"
                try:
                    with self._open_private_directory(source.parent) as source_parent_fd:
                        stage = stage_copy_at(
                            source_parent_fd,
                            source.name,
                            target_parent_fd,
                            stage_name,
                            expected_backup=source_expected,
                            symlink_policy=policy,
                        )
                except FileExistsError as error:
                    raise RuntimeError(
                        "Project runtime install stage collided with an existing path"
                    ) from error
                self._checkpoint_asset_receipt(
                    transaction,
                    spec.asset_id,
                    {
                        "installStageName": stage_name,
                        **self._identity_fields("installStage", stage),
                    },
                )
            else:
                stage_current = self._inspect_optional_asset_at(
                    target_parent_fd,
                    str(stage_name),
                    kind=spec.kind,
                    symlink_policy=policy,
                )
                current = self._inspect_optional_asset_at(
                    target_parent_fd,
                    spec.target.name,
                    kind=spec.kind,
                    symlink_policy=policy,
                )
                if stage_current is None and current != stage:
                    raise RuntimeError("Project runtime install stage is missing")
                if stage_current is not None and stage_current != stage:
                    raise RuntimeError("Project runtime install stage changed")

            assert stage is not None and isinstance(stage_name, str)
            if source_expected != self._project_runtime_source_identity(source, spec):
                raise RuntimeError("Qwen project runtime source changed before publication")
            self._assert_asset_parent_identity(target_parent_fd, spec, receipt)

            current = self._inspect_optional_asset_at(
                target_parent_fd,
                spec.target.name,
                kind=spec.kind,
                symlink_policy=policy,
            )
            if receipt.get("preExisted") is True:
                assert pre is not None
                if quarantine_name is None:
                    quarantine_name = f".qwen-recovery-quarantine-{uuid.uuid4().hex}"
                    self._checkpoint_asset_receipt(
                        transaction,
                        spec.asset_id,
                        {"installQuarantineName": quarantine_name},
                    )
                quarantine = self._inspect_optional_asset_at(
                    target_parent_fd,
                    quarantine_name,
                    kind=spec.kind,
                    symlink_policy=policy,
                )
                if quarantine is not None and quarantine != pre:
                    raise RuntimeError("Project runtime quarantine changed")
                if current is not None and current not in {pre, stage}:
                    raise RuntimeError("Project runtime target changed before quarantine")
                if current == pre:
                    quarantine_exact_at(
                        target_parent_fd,
                        spec.target.name,
                        quarantine_name,
                        expected=pre,
                        rename_noreplace=_rename_noreplace_at,
                        symlink_policy=policy,
                    )
                elif current is None:
                    if quarantine != pre:
                        raise RuntimeError("Project runtime preinstall asset is missing")
                self._checkpoint_asset_receipt(
                    transaction,
                    spec.asset_id,
                    {"installQuarantined": True},
                )
            elif current is not None and current != stage:
                raise RuntimeError("Project runtime target collision was preserved")

            if source_expected != self._project_runtime_source_identity(source, spec):
                raise RuntimeError("Qwen project runtime source changed before publication")
            self._assert_asset_parent_identity(target_parent_fd, spec, receipt)
            published = publish_noreplace_at(
                target_parent_fd,
                stage_name,
                spec.target.name,
                expected_stage=stage,
                rename_noreplace=_rename_noreplace_at,
                symlink_policy=policy,
            )
            if published != stage:
                raise RuntimeError("Project runtime publication identity changed")
            self._checkpoint_asset_receipt(
                transaction,
                spec.asset_id,
                {"installPublished": True},
            )
            self._assert_asset_parent_identity(target_parent_fd, spec, receipt)
            post = self._inspect_optional_asset_at(
                target_parent_fd,
                spec.target.name,
                kind=spec.kind,
                symlink_policy=policy,
            )
            if post != stage:
                raise RuntimeError("Project runtime post-publication identity changed")
            self._checkpoint_asset_receipt(
                transaction,
                spec.asset_id,
                {
                    "postParentDev": os.fstat(target_parent_fd).st_dev,
                    "postParentIno": os.fstat(target_parent_fd).st_ino,
                    **self._identity_fields("post", post),
                },
            )

            if receipt.get("preExisted") is True:
                assert pre is not None and isinstance(quarantine_name, str)
                quarantine = self._inspect_optional_asset_at(
                    target_parent_fd,
                    quarantine_name,
                    kind=spec.kind,
                    symlink_policy=policy,
                )
                if quarantine is not None:
                    if quarantine != pre:
                        raise RuntimeError("Project runtime quarantine changed before purge")
                    purge_exact_at(
                        target_parent_fd,
                        quarantine_name,
                        expected=pre,
                        symlink_policy=policy,
                    )
                self._checkpoint_asset_receipt(
                    transaction,
                    spec.asset_id,
                    {"installQuarantinePurged": True},
                )
            self._checkpoint_asset_receipt(
                transaction,
                spec.asset_id,
                {"installComplete": True},
            )

    def _validate_asset_receipt_structure(
        self, transaction: dict[str, Any], spec: RollbackAssetSpec,
        receipt: dict[str, Any], snapshot_dir: Path,
    ) -> tuple[AssetIdentity | None, AssetIdentity | None, AssetIdentity | None]:
        if receipt.get("canonicalParent") != str(Path(os.path.abspath(spec.target.parent))) \
                or receipt.get("canonicalName") != spec.target.name \
                or receipt.get("backupRelativeName") != spec.backup_relative.as_posix() \
                or receipt.get("mutationField") != spec.mutation_field \
                or type(receipt.get("preExisted")) is not bool \
                or type(receipt.get("mutationStarted")) is not bool:
            raise RuntimeError(f"Rollback asset receipt for {spec.asset_id} is malformed")
        expected_parent = self._asset_parent_identity(
            spec, receipt, require_owned=receipt["mutationStarted"],
        )
        stage_name = receipt.get("parentStageName")
        stage_dev = receipt.get("parentStageDev")
        stage_ino = receipt.get("parentStageIno")
        if any(value is not None for value in (stage_name, stage_dev, stage_ino)) and (
            not isinstance(stage_name, str)
            or ASSET_PARENT_STAGE_RE.fullmatch(stage_name) is None
            or type(stage_dev) is not int
            or type(stage_ino) is not int
        ):
            raise RuntimeError(f"Rollback asset parent transition for {spec.asset_id} is malformed")
        if receipt.get("parentPreExisted", receipt.get("parentDev") is not None) is True \
                and any(value is not None for value in (stage_name, stage_dev, stage_ino)):
            raise RuntimeError(f"Rollback asset parent transition for {spec.asset_id} is malformed")
        if receipt.get("parentCreated", False) is True and spec.asset_id in {"plugin", "skill"} \
                and (stage_dev, stage_ino) != expected_parent:
            raise RuntimeError(f"Rollback asset parent transition for {spec.asset_id} is malformed")
        post_parent_dev = receipt.get("postParentDev")
        post_parent_ino = receipt.get("postParentIno")
        if (post_parent_dev is None) != (post_parent_ino is None) \
                or post_parent_dev is not None and (
                    type(post_parent_dev) is not int or type(post_parent_ino) is not int
                    or (post_parent_dev, post_parent_ino) != expected_parent
                ):
            raise RuntimeError(f"Rollback asset post parent for {spec.asset_id} is malformed")
        if os.path.lexists(spec.target.parent):
            with self._open_private_directory(spec.target.parent) as parent_fd:
                self._assert_asset_parent_identity(parent_fd, spec, receipt)
        elif os.path.lexists(spec.target):
            raise RuntimeError(f"Rollback asset parent for {spec.asset_id} is unsafe")
        elif expected_parent is not None:
            raise RuntimeError(f"Rollback asset parent for {spec.asset_id} is missing")

        pre_existed = receipt["preExisted"]
        pre = self._receipt_identity(receipt, "pre", required=pre_existed)
        backup = self._receipt_identity(receipt, "backup", required=pre_existed)
        post = self._receipt_identity(receipt, "post", required=False)
        if pre_existed:
            assert pre is not None and backup is not None
            if pre.kind != spec.kind or backup.kind != spec.kind \
                    or pre.mode != backup.mode or pre.sha256 != backup.sha256:
                raise RuntimeError(f"Rollback asset backup for {spec.asset_id} is inconsistent")
            backup_path = snapshot_dir / spec.backup_relative
            if Path(os.path.abspath(backup_path)) != Path(os.path.abspath(
                snapshot_dir / Path(str(receipt["backupRelativeName"]))
            )):
                raise RuntimeError(f"Rollback asset backup for {spec.asset_id} escaped its snapshot")
            try:
                with self._open_private_directory(backup_path.parent) as backup_parent_fd:
                    actual_backup = inspect_asset_at(
                        backup_parent_fd,
                        backup_path.name,
                        kind=spec.kind,
                        symlink_policy=self._asset_symlink_policy_for(spec),
                    )
            except (OSError, RuntimeError, ValueError) as error:
                raise RuntimeError(f"Rollback asset backup for {spec.asset_id} is unsafe") from error
            if actual_backup != backup:
                raise RuntimeError(f"Rollback asset backup for {spec.asset_id} changed")
        elif pre is not None or backup is not None:
            raise RuntimeError(f"Rollback asset absence receipt for {spec.asset_id} is inconsistent")
        if post is not None and post.kind != spec.kind:
            raise RuntimeError(f"Rollback asset post identity for {spec.asset_id} is malformed")
        install_source = self._receipt_identity(
            receipt, "installSource", required=False,
        )
        install_stage = self._receipt_identity(
            receipt, "installStage", required=False,
        )
        install_stage_name = receipt.get("installStageName")
        install_quarantine_name = receipt.get("installQuarantineName")
        if install_source is not None and install_source.kind != spec.kind:
            raise RuntimeError(
                f"Project runtime source receipt for {spec.asset_id} is malformed"
            )
        if install_stage_name is not None and (
            not isinstance(install_stage_name, str)
            or ASSET_INSTALL_STAGE_RE.fullmatch(install_stage_name) is None
            or install_stage is None
        ):
            raise RuntimeError(
                f"Project runtime install stage for {spec.asset_id} is malformed"
            )
        if install_stage is not None and (
            not isinstance(install_stage_name, str) or install_stage.kind != spec.kind
        ):
            raise RuntimeError(
                f"Project runtime install stage for {spec.asset_id} is malformed"
            )
        if install_quarantine_name is not None and (
            not isinstance(install_quarantine_name, str)
            or QUARANTINE_RE.fullmatch(install_quarantine_name) is None
        ):
            raise RuntimeError(
                f"Project runtime quarantine for {spec.asset_id} is malformed"
            )
        for key in (
            "installQuarantined",
            "installPublished",
            "installQuarantinePurged",
            "installComplete",
        ):
            if key in receipt and type(receipt[key]) is not bool:
                raise RuntimeError(
                    f"Project runtime state for {spec.asset_id} is malformed"
                )
        return pre, backup, post

    def _preflight_rollback_assets(
        self, transaction: dict[str, Any], snapshot_dir: Path,
    ) -> dict[str, tuple[RollbackAssetSpec, dict[str, Any]]]:
        receipts = transaction.get("assetReceipts")
        if transaction.get("assetRecoverySchema") != ASSET_ROLLBACK_RECEIPT_SCHEMA \
                or not isinstance(receipts, dict):
            if any(transaction.get(spec.mutation_field) is True for spec in self._rollback_asset_specs()):
                raise RuntimeError("Current rollback asset receipts are missing")
            return {}
        prepared: dict[str, tuple[RollbackAssetSpec, dict[str, Any]]] = {}
        for spec in self._rollback_asset_specs():
            raw = receipts.get(spec.asset_id)
            global_started = transaction.get(spec.mutation_field) is True
            nested_started = isinstance(raw, dict) and raw.get("mutationStarted") is True
            transition_started = isinstance(raw, dict) and (
                raw.get("parentCreated") is True
                or raw.get("parentStageName") is not None
                or raw.get("parentStageDev") is not None
                or raw.get("parentStageIno") is not None
            )
            if isinstance(raw, dict) and (global_started or nested_started or transition_started):
                self._checkpoint_fresh_asset_parent(
                    transaction, spec, raw, create_if_missing=False,
                )
            if not global_started and not nested_started:
                continue
            if not isinstance(raw, dict) or raw.get("mutationStarted") is not True:
                raise RuntimeError(f"Rollback asset receipt for {spec.asset_id} is missing")
            pre, backup, post = self._validate_asset_receipt_structure(
                transaction, spec, raw, snapshot_dir,
            )
            current = self._inspect_optional_asset(spec)
            stage = self._receipt_identity(raw, "restoreStage", required=False)
            stage_name = raw.get("restoreStageName")
            if stage_name is not None and (
                not isinstance(stage_name, str)
                or ASSET_RESTORE_STAGE_RE.fullmatch(stage_name) is None
            ):
                raise RuntimeError(f"Rollback restore stage for {spec.asset_id} is malformed")
            if stage is not None and stage.kind != spec.kind:
                raise RuntimeError(f"Rollback restore stage for {spec.asset_id} is malformed")
            stage_current = self._inspect_optional_asset(spec, name=stage_name) \
                if isinstance(stage_name, str) else None
            if stage is not None and stage_current is not None and stage_current != stage:
                raise RuntimeError(f"Rollback restore stage for {spec.asset_id} changed")
            quarantine_name = raw.get("quarantineName")
            if quarantine_name is not None and (
                not isinstance(quarantine_name, str)
                or QUARANTINE_RE.fullmatch(quarantine_name) is None
            ):
                raise RuntimeError(f"Rollback quarantine for {spec.asset_id} is malformed")
            quarantine = self._inspect_optional_asset(spec, name=quarantine_name) \
                if isinstance(quarantine_name, str) else None
            if quarantine is not None and (post is None or quarantine != post):
                raise RuntimeError(f"Rollback quarantine for {spec.asset_id} changed")
            install_stage = self._receipt_identity(
                raw, "installStage", required=False,
            )
            install_stage_name = raw.get("installStageName")
            install_stage_current = self._inspect_optional_asset(
                spec, name=install_stage_name,
            ) if isinstance(install_stage_name, str) else None
            if install_stage_current is not None and install_stage_current != install_stage:
                raise RuntimeError(
                    f"Project runtime install stage for {spec.asset_id} changed"
                )
            install_quarantine_name = raw.get("installQuarantineName")
            install_quarantine = self._inspect_optional_asset(
                spec, name=install_quarantine_name,
            ) if isinstance(install_quarantine_name, str) else None
            if install_quarantine is not None and (
                pre is None or install_quarantine != pre
            ):
                raise RuntimeError(
                    f"Project runtime quarantine for {spec.asset_id} changed"
                )
            accepted = [
                candidate for candidate in (pre, post, stage, install_stage)
                if candidate is not None
            ]
            if current is not None and current not in accepted:
                raise RuntimeError(f"Rollback target for {spec.asset_id} changed")
            if current is not None and post is None \
                    and current != pre and current != stage and current != install_stage:
                raise RuntimeError(f"Rollback post identity for {spec.asset_id} is missing")
            if current is not None and not raw["preExisted"] \
                    and post is None and current != install_stage:
                raise RuntimeError(f"Rollback post identity for {spec.asset_id} is missing")
            if stage_name is not None and stage is None and stage_current is not None:
                if backup is None or stage_current.kind != backup.kind \
                        or stage_current.mode != backup.mode \
                        or stage_current.sha256 != backup.sha256:
                    raise RuntimeError(f"Rollback restore stage for {spec.asset_id} is unverified")
            prepared[spec.asset_id] = (spec, raw)
        return prepared

    def _rollback_one_asset(
        self,
        transaction: dict[str, Any],
        spec: RollbackAssetSpec,
        receipt: dict[str, Any],
        snapshot_dir: Path,
    ) -> None:
        pre, backup, post = self._validate_asset_receipt_structure(
            transaction, spec, receipt, snapshot_dir,
        )
        policy = self._asset_symlink_policy_for(spec)
        current = self._inspect_optional_asset(spec)
        quarantine_name = receipt.get("quarantineName")
        quarantine = self._inspect_optional_asset(spec, name=quarantine_name) \
            if isinstance(quarantine_name, str) else None
        install_stage_name = receipt.get("installStageName")
        install_stage = self._receipt_identity(
            receipt, "installStage", required=False,
        )
        install_stage_current = self._inspect_optional_asset(
            spec, name=install_stage_name,
        ) if isinstance(install_stage_name, str) else None
        install_quarantine_name = receipt.get("installQuarantineName")
        install_quarantine = self._inspect_optional_asset(
            spec, name=install_quarantine_name,
        ) if isinstance(install_quarantine_name, str) else None
        if install_stage_current is not None and install_stage_current != install_stage:
            raise RuntimeError(
                f"Project runtime install stage for {spec.asset_id} changed"
            )
        if install_quarantine is not None and (
            pre is None or install_quarantine != pre
        ):
            raise RuntimeError(
                f"Project runtime quarantine for {spec.asset_id} changed"
            )
        if current is not None and post is None and current == install_stage:
            assert install_stage is not None
            with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                parent = os.fstat(parent_fd)
            self._checkpoint_asset_receipt(
                transaction,
                spec.asset_id,
                {
                    "postParentDev": parent.st_dev,
                    "postParentIno": parent.st_ino,
                    **self._identity_fields("post", install_stage),
                },
            )
            post = install_stage

        if not receipt["preExisted"]:
            if current is not None:
                if post is None or current != post:
                    raise RuntimeError(f"Rollback target for {spec.asset_id} changed")
                if quarantine_name is None:
                    quarantine_name = f".qwen-recovery-quarantine-{uuid.uuid4().hex}"
                    self._checkpoint_asset_receipt(
                        transaction, spec.asset_id, {"quarantineName": quarantine_name},
                    )
                with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                    quarantine_exact_at(
                        parent_fd,
                        spec.target.name,
                        quarantine_name,
                        expected=post,
                        rename_noreplace=_rename_noreplace_at,
                        symlink_policy=policy,
                    )
                self._checkpoint_asset_receipt(
                    transaction, spec.asset_id, {"quarantined": True},
                )
                quarantine = post
            if quarantine is not None:
                if post is None or quarantine != post or not isinstance(quarantine_name, str):
                    raise RuntimeError(f"Rollback quarantine for {spec.asset_id} changed")
                with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                    purge_exact_at(
                        parent_fd,
                        quarantine_name,
                        expected=post,
                        symlink_policy=policy,
                    )
                self._checkpoint_asset_receipt(
                    transaction, spec.asset_id, {"quarantinePurged": True},
                )
            with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                if self._inspect_optional_asset_at(
                    parent_fd,
                    spec.target.name,
                    kind=spec.kind,
                    symlink_policy=policy,
                ) is not None:
                    raise RuntimeError(f"Rollback target for {spec.asset_id} was not removed")
            install_stage_current = self._inspect_optional_asset(
                spec, name=install_stage_name,
            ) if isinstance(install_stage_name, str) else None
            if install_stage_current is not None:
                if install_stage is None or install_stage_current != install_stage:
                    raise RuntimeError(
                        f"Project runtime install stage for {spec.asset_id} changed"
                    )
                with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                    purge_exact_at(
                        parent_fd,
                        install_stage_name,
                        expected=install_stage,
                        symlink_policy=policy,
                    )
                self._checkpoint_asset_receipt(
                    transaction,
                    spec.asset_id,
                    {"rollbackInstallStagePurged": True},
                )
            self._checkpoint_asset_receipt(
                transaction, spec.asset_id, {"rollbackComplete": True},
            )
            return

        assert pre is not None and backup is not None
        backup_path = snapshot_dir / spec.backup_relative
        stage_name = receipt.get("restoreStageName")
        if stage_name is None:
            stage_name = f".qwen-asset-restore-{uuid.uuid4().hex}"
            self._checkpoint_asset_receipt(
                transaction, spec.asset_id, {"restoreStageName": stage_name},
            )
        if not isinstance(stage_name, str) \
                or ASSET_RESTORE_STAGE_RE.fullmatch(stage_name) is None:
            raise RuntimeError(f"Rollback restore stage for {spec.asset_id} is malformed")
        stage = self._receipt_identity(receipt, "restoreStage", required=False)
        stage_current = self._inspect_optional_asset(spec, name=stage_name)
        if stage is None:
            if stage_current is None:
                with self._open_private_directory(backup_path.parent) as backup_parent_fd, \
                        self._open_checked_asset_parent(spec, receipt) as target_parent_fd:
                    stage_current = stage_copy_at(
                        backup_parent_fd,
                        backup_path.name,
                        target_parent_fd,
                        stage_name,
                        expected_backup=backup,
                        symlink_policy=policy,
                    )
            elif stage_current.kind != backup.kind or stage_current.mode != backup.mode \
                    or stage_current.sha256 != backup.sha256:
                raise RuntimeError(f"Rollback restore stage for {spec.asset_id} is unverified")
            stage = stage_current
            self._checkpoint_asset_receipt(
                transaction,
                spec.asset_id,
                self._identity_fields("restoreStage", stage),
            )
        elif stage_current is not None and stage_current != stage:
            raise RuntimeError(f"Rollback restore stage for {spec.asset_id} changed")

        current = self._inspect_optional_asset(spec)
        restored_already = current == pre or current == stage
        if current is not None and not restored_already:
            if post is None or current != post:
                raise RuntimeError(f"Rollback target for {spec.asset_id} changed")
            if quarantine_name is None:
                quarantine_name = f".qwen-recovery-quarantine-{uuid.uuid4().hex}"
                self._checkpoint_asset_receipt(
                    transaction, spec.asset_id, {"quarantineName": quarantine_name},
                )
            with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                quarantine_exact_at(
                    parent_fd,
                    spec.target.name,
                    quarantine_name,
                    expected=post,
                    rename_noreplace=_rename_noreplace_at,
                    symlink_policy=policy,
                )
            self._checkpoint_asset_receipt(
                transaction, spec.asset_id, {"quarantined": True},
            )
            quarantine = post
            current = None

        if current is None:
            install_quarantine = self._inspect_optional_asset(
                spec, name=install_quarantine_name,
            ) if isinstance(install_quarantine_name, str) else None
            if install_quarantine is not None:
                if install_quarantine != pre:
                    raise RuntimeError(
                        f"Project runtime quarantine for {spec.asset_id} changed"
                    )
                with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                    restored = publish_noreplace_at(
                        parent_fd,
                        install_quarantine_name,
                        spec.target.name,
                        expected_stage=pre,
                        rename_noreplace=_rename_noreplace_at,
                        symlink_policy=policy,
                    )
                if restored != pre:
                    raise RuntimeError(
                        f"Project runtime quarantine restoration for {spec.asset_id} changed"
                    )
                self._checkpoint_asset_receipt(
                    transaction,
                    spec.asset_id,
                    {"rollbackInstallQuarantineRestored": True},
                )
                current = restored
            else:
                with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                    restored = publish_noreplace_at(
                        parent_fd,
                        stage_name,
                        spec.target.name,
                        expected_stage=stage,
                        rename_noreplace=_rename_noreplace_at,
                        symlink_policy=policy,
                    )
                if restored != stage:
                    raise RuntimeError(f"Rollback restoration for {spec.asset_id} changed")
                current = restored
        if current != pre and current != stage:
            raise RuntimeError(f"Rollback restoration for {spec.asset_id} is incomplete")
        self._checkpoint_asset_receipt(
            transaction, spec.asset_id, {"rollbackPublished": True},
        )

        if current == pre and stage_current is not None:
            with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                purge_exact_at(
                    parent_fd,
                    stage_name,
                    expected=stage,
                    symlink_policy=policy,
                )
            self._checkpoint_asset_receipt(
                transaction, spec.asset_id, {"restoreStagePurged": True},
            )

        quarantine_name = receipt.get("quarantineName")
        quarantine = self._inspect_optional_asset(spec, name=quarantine_name) \
            if isinstance(quarantine_name, str) else None
        if quarantine is not None:
            if post is None or quarantine != post or not isinstance(quarantine_name, str):
                raise RuntimeError(f"Rollback quarantine for {spec.asset_id} changed")
            with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                purge_exact_at(
                    parent_fd,
                    quarantine_name,
                    expected=post,
                    symlink_policy=policy,
                )
            self._checkpoint_asset_receipt(
                transaction, spec.asset_id, {"quarantinePurged": True},
            )
        install_stage_current = self._inspect_optional_asset(
            spec, name=install_stage_name,
        ) if isinstance(install_stage_name, str) else None
        if install_stage_current is not None:
            if install_stage is None or install_stage_current != install_stage:
                raise RuntimeError(
                    f"Project runtime install stage for {spec.asset_id} changed"
                )
            with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                purge_exact_at(
                    parent_fd,
                    install_stage_name,
                    expected=install_stage,
                    symlink_policy=policy,
                )
            self._checkpoint_asset_receipt(
                transaction,
                spec.asset_id,
                {"rollbackInstallStagePurged": True},
            )
        install_quarantine = self._inspect_optional_asset(
            spec, name=install_quarantine_name,
        ) if isinstance(install_quarantine_name, str) else None
        if install_quarantine is not None:
            if install_quarantine != pre:
                raise RuntimeError(
                    f"Project runtime quarantine for {spec.asset_id} changed"
                )
            with self._open_checked_asset_parent(spec, receipt) as parent_fd:
                purge_exact_at(
                    parent_fd,
                    install_quarantine_name,
                    expected=pre,
                    symlink_policy=policy,
                )
            self._checkpoint_asset_receipt(
                transaction,
                spec.asset_id,
                {"rollbackInstallQuarantinePurged": True},
            )
        with self._open_checked_asset_parent(spec, receipt) as parent_fd:
            final = self._inspect_optional_asset_at(
                parent_fd,
                spec.target.name,
                kind=spec.kind,
                symlink_policy=policy,
            )
        if final != pre and final != stage:
            raise RuntimeError(f"Rollback restoration for {spec.asset_id} did not verify")
        self._checkpoint_asset_receipt(
            transaction, spec.asset_id, {"rollbackComplete": True},
        )

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
            if run_identity is not None:
                try:
                    if run_marker_sha256 is not None:
                        self._remove_snapshot_run(run_dir, run_identity, run_marker_sha256)
                    else:
                        with self._open_private_directory(snapshot_root) as snapshot_fd:
                            self._remove_tree_at(snapshot_fd, run_dir.name, run_identity)
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

    def _checkpoint_mutation(self, transaction: dict[str, Any] | None, field: str) -> None:
        if transaction is None or transaction.get(field) is True:
            return
        transaction[field] = True
        self.store.write(transaction)

    def configure_openclaw(
        self, allowed_projects: list[str], *, transaction: dict[str, Any] | None = None,
    ) -> None:
        plugin_archive = self.package_plugin_archive()
        try:
            self._checkpoint_asset_mutation(transaction, ["plugin"])
            self._checkpoint_mutation(transaction, "configMutationStarted")
            self.cli.run(["plugins", "install", "--force", str(plugin_archive)], timeout=600)
            self._capture_asset_post_identity(transaction, "plugin")
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
        tool_allow_path, tool_allow = resolve_tool_allowlist_update(
            self.cli.config_get("tools.allow"), self.cli.config_get("tools.alsoAllow"), TOOL_NAME
        )
        if tool_allow_path is not None and tool_allow is not None:
            self.cli.run(["config", "set", tool_allow_path, json.dumps(tool_allow),
                          "--strict-json", "--replace"])
        self._checkpoint_asset_mutation(transaction, ["skill"])
        self.cli.run(["skills", "install", str(self.skill_source), "--as", SKILL_ID, "--force", "--agent", self.agent], timeout=300)
        self._capture_asset_post_identity(transaction, "skill")
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

    @property
    def ownership_manifest(self) -> Path:
        return self.store.manifest_path

    @property
    def health_receipt_path(self) -> Path:
        return self.paths.project_root / "reports/backup-health-component.qwen-local.json"

    def _ownership_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": OWNERSHIP_SCHEMA,
            "contractVersion": INTEGRATION_CONTRACT_VERSION,
            "snapshotContract": SNAPSHOT_CONTRACT,
            "provider": "qwen-local",
            "localOnly": True,
            "projectRoot": str(self.paths.project_root),
            "snapshotRoot": str(self.snapshot_root),
            "healthReceiptPath": str(self.health_receipt_path),
            "snapshotScriptPath": str(self.paths.project_root / "scripts/snapshot_knowledge_assets.py"),
            "snapshotWrapperPath": str(self.paths.project_root / "scripts/run_verified_snapshot.py"),
            "indexLockPath": str(self.paths.project_root / "data/index.lock"),
            "timezone": self.timezone_name,
            "tableName": "knowledge_chunks_qwen_local_768",
            "incrementalDeclarationKey": CRON_DECLARATION_KEY,
            "snapshotDeclarationKey": SNAPSHOT_CRON_DECLARATION_KEY,
            "initialDeclarationKey": INITIAL_CRON_DECLARATION_KEY,
            "healthReceiptSchema": HEALTH_RECEIPT_SCHEMA,
            "reportChannel": self.report_channel,
            "reportTo": self.report_to,
            "reportAccountId": self.report_account_id,
        }
        if self.approved_disabled_collision is not None:
            payload["approvedDisabledCollision"] = self.approved_disabled_collision.receipt()
        return payload

    def _prepare_owned_root(
        self, transaction: dict[str, Any], *, root: Path, prefix: str,
        planned_key: str, created_key: str, stage_name: str,
    ) -> bool:
        planned = transaction.get(planned_key)
        if type(planned) is not bool or transaction.get(created_key) is not False:
            raise RuntimeError("Managed root creation does not match the transaction plan")
        if not planned:
            if not os.path.lexists(root):
                raise RuntimeError("Pre-existing managed root disappeared after preflight")
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                metadata = os.fstat(root_fd)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise RuntimeError("Pre-existing managed root is unsafe")
            finally:
                os.close(root_fd)
            return False
        if os.path.lexists(root):
            raise RuntimeError("Fresh managed root appeared after integration preflight")
        with self._open_private_directory(root.parent, create=True) as parent_fd:
            stage_fd: int | None = None
            stage_identity: tuple[int, int] | None = None
            published = False
            try:
                parent_meta = os.fstat(parent_fd)
                if not stat.S_ISDIR(parent_meta.st_mode) or parent_meta.st_uid != os.getuid() \
                        or parent_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise RuntimeError("Managed root parent is unsafe")
                os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
                stage_fd = os.open(
                    stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                metadata = os.fstat(stage_fd)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                        or metadata.st_mode & 0o077:
                    raise RuntimeError("Fresh managed root staging directory is unsafe")
                stage_identity = (metadata.st_dev, metadata.st_ino)
                transaction.update({
                    f"{prefix}StageName": stage_name,
                    f"{prefix}StageDev": metadata.st_dev,
                    f"{prefix}StageIno": metadata.st_ino,
                    f"{prefix}Published": False,
                })
                self.store.write(transaction)
                os.fsync(stage_fd)
                os.fsync(parent_fd)
                _rename_noreplace_at(parent_fd, stage_name, parent_fd, root.name)
                published = True
                os.fsync(parent_fd)
                final_meta = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISDIR(final_meta.st_mode) or final_meta.st_uid != os.getuid() \
                        or final_meta.st_mode & 0o077 \
                        or (final_meta.st_dev, final_meta.st_ino) != stage_identity:
                    raise RuntimeError("Published managed root identity changed")
                transaction.update({
                    f"{prefix}Published": True,
                    created_key: True,
                    f"{prefix}Dev": final_meta.st_dev,
                    f"{prefix}Ino": final_meta.st_ino,
                })
                self.store.write(transaction)
                return True
            finally:
                cleanup_error: Exception | None = None
                if stage_identity is not None and not published:
                    try:
                        self._quarantine_transaction_path(
                            transaction,
                            receipt_prefix=prefix,
                            parent_fd=parent_fd,
                            name=stage_name,
                            expected_identity=stage_identity,
                            directory=True,
                        )
                    except Exception as error:
                        cleanup_error = error
                if stage_fd is not None:
                    try:
                        os.close(stage_fd)
                    except Exception as error:
                        if cleanup_error is None:
                            cleanup_error = error
                if cleanup_error is not None:
                    raise RuntimeError("Managed root staging cleanup was incomplete") from cleanup_error

    def _prepare_snapshot_root(self, transaction: dict[str, Any]) -> bool:
        return self._prepare_owned_root(
            transaction,
            root=self.snapshot_root,
            prefix="snapshotRoot",
            planned_key="snapshotRootCreatePlanned",
            created_key="snapshotRootCreated",
            stage_name=f".qwen-snapshot-root.install-{uuid.uuid4().hex}",
        )

    def _prepare_project_root(self, transaction: dict[str, Any]) -> None:
        """Create and durably receipt a fresh managed project before bootstrap can block."""
        if transaction.get("projectExisted") is True \
                or transaction.get("projectCreatePlanned") is not True:
            raise RuntimeError("Fresh project creation does not match the transaction plan")
        if transaction.get("projectCreated") is not False \
                or "projectRootDev" in transaction or "projectRootIno" in transaction:
            raise RuntimeError("Fresh project ownership was already checkpointed")
        self._prepare_owned_root(
            transaction,
            root=self.paths.project_root,
            prefix="projectRoot",
            planned_key="projectCreatePlanned",
            created_key="projectCreated",
            stage_name=f".qwen-project-root.install-{uuid.uuid4().hex}",
        )

    def _incremental_spec(self) -> ManagedCronSpec:
        script = self.paths.project_root / "scripts/knowledge_index_incremental.sh"
        return ManagedCronSpec(
            key=CRON_DECLARATION_KEY,
            name="Qwen local knowledge incremental index",
            description=INCREMENTAL_CRON_DESCRIPTION,
            schedule="30 6 * * *",
            timezone=self.timezone_name,
            argv=(str(script), str(self.ownership_manifest)),
            cwd=str(self.paths.project_root),
            timeout_seconds=7200,
            no_output_timeout_seconds=900,
            output_max_bytes=65536,
            report_channel=self.report_channel,
            report_to=str(self.report_to),
            report_account_id=self.report_account_id,
            command_env=(("QWEN_OWNERSHIP_MANIFEST", str(self.ownership_manifest)),
                         ("QWEN_PYTHON", str(self.python_path)),
                         ("OPENCLAW_LANCEDB_ROOT", str(self.paths.project_root))),
        )

    def _snapshot_spec(self) -> ManagedCronSpec:
        wrapper = self.paths.project_root / "scripts/run_verified_snapshot.py"
        return ManagedCronSpec(
            key=SNAPSHOT_CRON_DECLARATION_KEY,
            name="Qwen local verified recovery snapshot",
            description=SNAPSHOT_CRON_DESCRIPTION,
            schedule="50 6 * * *",
            timezone=self.timezone_name,
            argv=(str(self.python_path), str(wrapper), "--ownership-manifest", str(self.ownership_manifest)),
            cwd=str(self.paths.project_root),
            timeout_seconds=7200,
            no_output_timeout_seconds=3600,
            output_max_bytes=16384,
            report_channel=self.report_channel,
            report_to=str(self.report_to),
            report_account_id=self.report_account_id,
        )

    def _inventory(self) -> list[dict[str, Any]]:
        return _cron_jobs(self.cli.json(["cron", "list", "--all", "--json"]))

    def _owned_gemini_jobs_exact(self, jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        expected = self.paths.workspace / "knowledge-lancedb/scripts/knowledge_index_incremental.sh"
        candidates = [
            job for job in jobs
            if job.get("declarationKey") == GEMINI_DECLARATION_KEY
        ]
        if any(
            _job_argv(job) != [str(expected)]
            or not _job_targets_exact_script(job, expected)
            or not _default_cron_behavior_contract(job)
            or (job.get("payload") if isinstance(job.get("payload"), dict) else {}).get(
                "env"
            ) not in (None, {})
            for job in candidates
        ):
            raise RuntimeError("Existing owned Gemini cron is outside the safe upgrade allowlist")
        for job in candidates:
            try:
                _job_definition(job)
            except RuntimeError as error:
                raise RuntimeError(
                    "Existing owned Gemini cron is outside the safe upgrade allowlist"
                ) from error
        return candidates

    @staticmethod
    def _inventory_hashes(jobs: list[dict[str, Any]]) -> dict[str, str]:
        return {
            str(job["id"]): _job_contract_hash(job, include_id=True)
            for job in jobs
        }

    def _edit_cron_with_snapshot_guard(
        self,
        job_id: str,
        args: list[str],
        *,
        before: Callable[[dict[str, Any]], bool],
        after: Callable[[dict[str, Any]], bool],
        label: str,
    ) -> None:
        """Bound an ID-based cron edit to exact before/after full-inventory snapshots."""
        if not SAFE_CRON_JOB_ID_RE.fullmatch(job_id) \
                or args[:3] != ["cron", "edit", job_id]:
            raise RuntimeError(f"{label} cron edit authority is malformed")
        inventory_before = self._inventory()
        before_by_id = {str(job["id"]): job for job in inventory_before}
        candidate = before_by_id.get(job_id)
        if candidate is None or not before(candidate):
            raise RuntimeError(f"{label} cron identity was reused or drifted before edit")
        hashes_before = self._inventory_hashes(inventory_before)
        if self._inventory_hashes(self._inventory()) != hashes_before:
            raise RuntimeError(f"{label} cron inventory changed before edit")
        self.cli.run(args)
        inventory_after = self._inventory()
        after_by_id = {str(job["id"]): job for job in inventory_after}
        edited = after_by_id.get(job_id)
        if edited is None or not after(edited):
            raise RuntimeError(f"{label} cron contract drifted during edit")
        hashes_after = self._inventory_hashes(inventory_after)
        if set(hashes_after) != set(hashes_before) or any(
            hashes_after[other_id] != fingerprint
            for other_id, fingerprint in hashes_before.items()
            if other_id != job_id
        ):
            raise RuntimeError(f"{label} cron edit changed another inventory entry")

    @staticmethod
    def _runtime_job_active(job: dict[str, Any]) -> bool:
        if "state" in job:
            raw_state = job["state"]
            if not isinstance(raw_state, dict):
                raise RuntimeError("Cron runtime state is malformed")
            state = raw_state
        else:
            state = {}
        if "runningAtMs" in state:
            running_at = state["runningAtMs"]
            if type(running_at) is not int or running_at <= 0:
                raise RuntimeError("Cron running timestamp is malformed")
            return True
        state_status: str | None = None
        top_status: str | None = None
        if "status" in state:
            state_status = state["status"]
            if not isinstance(state_status, str) or not state_status.strip():
                raise RuntimeError("Cron runtime status is malformed")
        if "status" in job:
            top_status = job["status"]
            if not isinstance(top_status, str) or not top_status.strip():
                raise RuntimeError("Cron runtime status is malformed")
        return state_status in {"running", "starting"} \
            or top_status in {"running", "starting"}

    def _wait_for_quiesced_jobs(self, job_ids: set[str], *, timeout_seconds: float = 1800,
                                poll_seconds: float = 1.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            jobs = self._inventory()
            active = [
                job for job in jobs
                if str(job.get("id")) in job_ids and self._runtime_job_active(job)
            ]
            if not active:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Owned cron execution did not quiesce before the bounded deadline")
            time.sleep(min(max(0.01, poll_seconds), max(0.01, deadline - time.monotonic())))

    def _quiesce_rollback_removals(
        self, current_jobs: list[dict[str, Any]], removable_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Disable every present removal target, then wait for live runs to finish."""
        current_by_id = {str(job["id"]): job for job in current_jobs}
        present_ids = removable_ids & set(current_by_id)
        for job_id in sorted(present_ids):
            original = current_by_id[job_id]
            enabled = original.get("enabled")
            if type(enabled) is not bool:
                raise RuntimeError("Rollback removal target enabled state is malformed")
            if enabled:
                expected_disabled = json.loads(json.dumps(original))
                expected_disabled["enabled"] = False
                self._edit_cron_with_snapshot_guard(
                    job_id,
                    ["cron", "edit", job_id, "--disable"],
                    before=lambda job, expected=original: _job_contract_hash(
                        job, include_id=True,
                    ) == _job_contract_hash(expected, include_id=True),
                    after=lambda job, expected=expected_disabled: _job_contract_hash(
                        job, include_id=True,
                    ) == _job_contract_hash(expected, include_id=True),
                    label="Rollback removal quiescence",
                )
                current_by_id[job_id] = expected_disabled
        expected_hashes = {
            job_id: _job_contract_hash(job, include_id=True)
            for job_id, job in current_by_id.items()
        }
        disabled_inventory = self._inventory()
        if self._inventory_hashes(disabled_inventory) != expected_hashes:
            raise RuntimeError("Cron inventory drifted during rollback quiescence")
        self._wait_for_quiesced_jobs(present_ids)
        quiesced_inventory = self._inventory()
        if self._inventory_hashes(quiesced_inventory) != expected_hashes:
            raise RuntimeError("Cron inventory drifted while rollback waited for active runs")
        return quiesced_inventory

    def _quiesce_prior_jobs(self, jobs_before: list[dict[str, Any]], target_ids: set[str],
                            inventory_hashes_before: dict[str, str]) -> list[str]:
        current = self._inventory()
        if self._inventory_hashes(current) != inventory_hashes_before:
            raise RuntimeError("Cron inventory changed between preflight and quiescence")
        original_by_id = {str(job["id"]): job for job in jobs_before}
        enabled_before: list[str] = []
        for job_id in sorted(target_ids):
            original = original_by_id.get(job_id)
            if original is None:
                raise RuntimeError("Cron quiescence target disappeared after preflight")
            if original.get("enabled", True) is True:
                expected_disabled = json.loads(json.dumps(original))
                expected_disabled["enabled"] = False
                self._edit_cron_with_snapshot_guard(
                    job_id,
                    ["cron", "edit", job_id, "--disable"],
                    before=lambda job, expected=original: _job_contract_hash(
                        job, include_id=True,
                    ) == _job_contract_hash(expected, include_id=True),
                    after=lambda job, expected=expected_disabled: _job_contract_hash(
                        job, include_id=True,
                    ) == _job_contract_hash(expected, include_id=True),
                    label="Owned quiescence",
                )
                enabled_before.append(job_id)
        self._wait_for_quiesced_jobs(target_ids)
        after = self._inventory()
        after_by_id = {str(job["id"]): job for job in after}
        for job_id in target_ids:
            current_job = after_by_id.get(job_id)
            if current_job is None:
                raise RuntimeError("Cron quiescence target disappeared during disable")
            expected = dict(original_by_id[job_id])
            expected["enabled"] = False
            if _job_contract_hash(current_job, include_id=True) != _job_contract_hash(expected, include_id=True):
                raise RuntimeError("Cron quiescence changed more than the enabled state")
        unknown_before = {
            job_id: fingerprint for job_id, fingerprint in inventory_hashes_before.items()
            if job_id not in target_ids
        }
        unknown_after = {
            str(job["id"]): _job_contract_hash(job, include_id=True)
            for job in after if str(job["id"]) not in target_ids
        }
        if unknown_after != unknown_before:
            raise RuntimeError("Unknown cron definitions changed during owned-job quiescence")
        return enabled_before

    def _remove_prior_managed_jobs_for_replacement(
        self,
        jobs_before: list[dict[str, Any]],
        target_ids: set[str],
        inventory_hashes_before: dict[str, str],
    ) -> list[str]:
        """Remove only write-ahead-receipted managed IDs after exact quiescence readback."""
        transaction = self.store.read()
        raw_receipt = transaction.get("cronReplaceIdsBefore")
        if transaction.get("phase") != "replacing_managed_cron" \
                or not isinstance(raw_receipt, list) \
                or any(
                    not isinstance(value, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(value)
                    for value in raw_receipt
                ) \
                or len(raw_receipt) != len(set(raw_receipt)):
            raise RuntimeError("Cron replacement durable authority is missing or malformed")
        self._validate_replacement_receipt_graph(transaction)
        persisted_hashes = transaction.get("cronInventoryHashesBefore")
        if persisted_hashes != inventory_hashes_before:
            raise RuntimeError("Cron replacement inventory receipt drifted")
        if self._inventory_hashes(jobs_before) != inventory_hashes_before:
            raise RuntimeError("Cron replacement preflight receipt is inconsistent")
        original_by_id = {str(job["id"]): job for job in jobs_before}
        replace_ids = sorted(
            job_id for job_id, job in original_by_id.items()
            if job.get("declarationKey") in MANAGED_CRON_KEYS
        )
        if raw_receipt != replace_ids:
            raise RuntimeError("Cron replacement ids do not match durable authority")
        if any(job_id not in target_ids for job_id in replace_ids):
            raise RuntimeError("Cron replacement target is outside the quiesced ownership set")

        expected_quiesced: dict[str, str] = {}
        for job_id, original in original_by_id.items():
            expected = dict(original)
            if job_id in target_ids:
                expected["enabled"] = False
            expected_quiesced[job_id] = _job_contract_hash(expected, include_id=True)
        current_quiesced = self._inventory()
        if self._inventory_hashes(current_quiesced) != expected_quiesced:
            raise RuntimeError("Cron inventory changed before managed replacement")
        self._remove_cron_ids_with_snapshot_guard(
            current_quiesced, set(replace_ids),
        )

        expected_remaining = {
            job_id: fingerprint for job_id, fingerprint in expected_quiesced.items()
            if job_id not in replace_ids
        }
        after = self._inventory()
        if self._inventory_hashes(after) != expected_remaining:
            raise RuntimeError("Cron inventory changed during managed replacement")
        if any(job.get("declarationKey") in MANAGED_CRON_KEYS for job in after):
            raise RuntimeError("Prior managed cron declaration remained after replacement removal")
        return replace_ids

    def _verify_pre_legacy_removal_inventory(
        self, transaction: dict[str, Any], jobs: list[dict[str, Any]],
    ) -> None:
        """Bind legacy deletion to the complete durable post-staging topology."""
        if transaction.get("phase") != "staging_managed_cron" \
                or transaction.get("cronContractHashVersion") != CRON_CONTRACT_HASH_VERSION:
            raise RuntimeError("Legacy cron removal phase is not durably authorized")
        legacy_ids = transaction.get("cronLegacyRemoveIdsBefore")
        definitions = transaction.get("cronDefinitionsBefore")
        unknown_hashes = transaction.get("cronUnknownHashesBefore")
        gemini_hashes = transaction.get("cronPreservedGeminiHashesAfterQuiesce")
        receipts = (unknown_hashes, gemini_hashes)
        if not isinstance(legacy_ids, list) \
                or any(
                    not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    for job_id in legacy_ids
                ) \
                or len(legacy_ids) != len(set(legacy_ids)) \
                or not isinstance(definitions, list) \
                or any(not isinstance(item, dict) for item in definitions) \
                or any(
                    not isinstance(receipt, dict)
                    or any(
                        not isinstance(job_id, str)
                        or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                        or not isinstance(fingerprint, str)
                        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                        for job_id, fingerprint in receipt.items()
                    )
                    for receipt in receipts
                ):
            raise RuntimeError("Legacy cron removal receipt graph is malformed")
        assert isinstance(unknown_hashes, dict) and isinstance(gemini_hashes, dict)
        expected_hashes = dict(unknown_hashes)
        if set(expected_hashes) & set(gemini_hashes):
            raise RuntimeError("Legacy cron removal receipt authority overlaps")
        expected_hashes.update(gemini_hashes)

        legacy_id_set = set(legacy_ids)
        definitions_by_id = {
            str(definition.get("id")): _job_definition(definition)
            for definition in definitions
            if str(definition.get("id")) in legacy_id_set
        }
        if set(definitions_by_id) != legacy_id_set:
            raise RuntimeError("Legacy cron removal definitions are incomplete")
        for job_id, definition in definitions_by_id.items():
            disabled = json.loads(json.dumps(definition))
            disabled["enabled"] = False
            if job_id in expected_hashes:
                raise RuntimeError("Legacy cron removal receipt authority overlaps")
            expected_hashes[job_id] = _job_contract_hash(disabled, include_id=True)

        intents = self._validated_cron_intent_receipts(
            transaction, "cronStagingIntents",
        )
        expected_intents = {CRON_DECLARATION_KEY, SNAPSHOT_CRON_DECLARATION_KEY}
        if set(intents) != expected_intents:
            raise RuntimeError("Legacy cron removal contains an unexpected staging intent")
        for declaration_key in sorted(expected_intents):
            intent = intents[declaration_key]
            if intent.get("configured") is not True:
                raise RuntimeError("Legacy cron removal lacks configured managed intent")
            job_id = intent.get("jobId")
            if not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id) \
                    or job_id in expected_hashes:
                raise RuntimeError("Legacy cron removal managed id authority is invalid")
            configured_disabled = json.loads(json.dumps(
                self._managed_intent_lifecycle_contracts(intent)[2]
            ))
            configured_disabled["id"] = job_id
            expected_hashes[job_id] = _job_contract_hash(
                configured_disabled, include_id=True,
            )
        if self._inventory_hashes(jobs) != expected_hashes:
            raise RuntimeError("Cron inventory drifted before legacy removal")

    @staticmethod
    def _validate_index_lock(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or metadata.st_mode & 0o077:
            raise RuntimeError("Qwen index lock is unsafe")

    @staticmethod
    def _validate_snapshot_lock(metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or metadata.st_nlink != 1 or metadata.st_mode & 0o077 or metadata.st_size != 0:
            raise RuntimeError("Qwen snapshot run lock is unsafe")

    def _quarantine_exact_path(
        self, parent_fd: int, name: str, expected_identity: tuple[int, int], *,
        directory: bool, quarantine_name: str | None = None,
    ) -> str | None:
        if quarantine_name is None:
            quarantine_name = f".qwen-recovery-quarantine-{uuid.uuid4().hex}"
        if QUARANTINE_RE.fullmatch(quarantine_name) is None:
            raise RuntimeError("Qwen recovery quarantine identity is unsafe")

        def validate(metadata: os.stat_result) -> None:
            if directory:
                self._validate_index_lock(metadata)
            else:
                self._validate_snapshot_lock(metadata)

        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                quarantined = os.stat(
                    quarantine_name, dir_fd=parent_fd, follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            validate(quarantined)
            if (quarantined.st_dev, quarantined.st_ino) != expected_identity:
                raise RuntimeError("Qwen recovery quarantine identity changed")
            return quarantine_name
        validate(metadata)
        if (metadata.st_dev, metadata.st_ino) != expected_identity:
            raise RuntimeError("Qwen runtime lock identity changed")
        try:
            _rename_noreplace_at(parent_fd, name, parent_fd, quarantine_name)
        except FileNotFoundError:
            return self._quarantine_exact_path(
                parent_fd,
                name,
                expected_identity,
                directory=directory,
                quarantine_name=quarantine_name,
            )
        moved = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
        try:
            validate(moved)
            if (moved.st_dev, moved.st_ino) != expected_identity:
                raise RuntimeError("Qwen recovery candidate changed during quarantine")
        except Exception as validation_error:
            try:
                _rename_noreplace_at(parent_fd, quarantine_name, parent_fd, name)
            except Exception as restore_error:
                raise RuntimeError(
                    "Qwen recovery candidate changed and was preserved in quarantine"
                ) from restore_error
            raise validation_error
        os.fsync(parent_fd)
        return quarantine_name

    def _quarantine_transaction_path(
        self, transaction: dict[str, Any], *, receipt_prefix: str, parent_fd: int,
        name: str, expected_identity: tuple[int, int], directory: bool,
    ) -> bool:
        name_key = f"{receipt_prefix}QuarantineName"
        state_key = f"{receipt_prefix}Quarantined"
        quarantine_name = transaction.get(name_key)
        if quarantine_name is None:
            quarantine_name = f".qwen-recovery-quarantine-{uuid.uuid4().hex}"
            transaction[name_key] = quarantine_name
            self.store.write(transaction)
        elif not isinstance(quarantine_name, str) \
                or QUARANTINE_RE.fullmatch(quarantine_name) is None:
            raise RuntimeError("Qwen recovery quarantine receipt is malformed")
        moved = self._quarantine_exact_path(
            parent_fd,
            name,
            expected_identity,
            directory=directory,
            quarantine_name=quarantine_name,
        )
        if moved is not None:
            if transaction.get(state_key) not in {None, True}:
                raise RuntimeError("Qwen recovery quarantine state is not monotonic")
            transaction[state_key] = True
            self.store.write(transaction)
            return True
        return False

    def _quarantine_checkpointed_path(
        self, receipt: dict[str, Any], *, receipt_prefix: str, parent_fd: int,
        name: str, expected_identity: tuple[int, int], directory: bool,
        checkpoint: Callable[[dict[str, Any]], None],
    ) -> bool:
        name_key = f"{receipt_prefix}QuarantineName"
        state_key = f"{receipt_prefix}Quarantined"
        quarantine_name = receipt.get(name_key)
        if quarantine_name is None:
            quarantine_name = f".qwen-recovery-quarantine-{uuid.uuid4().hex}"
            checkpoint({name_key: quarantine_name})
            receipt[name_key] = quarantine_name
        elif not isinstance(quarantine_name, str) \
                or QUARANTINE_RE.fullmatch(quarantine_name) is None:
            raise RuntimeError("Qwen recovery quarantine receipt is malformed")
        moved = self._quarantine_exact_path(
            parent_fd,
            name,
            expected_identity,
            directory=directory,
            quarantine_name=quarantine_name,
        )
        if moved is not None:
            checkpoint({state_key: True})
            receipt[state_key] = True
            return True
        return False

    def _purge_exact_quarantine(
        self, parent_fd: int, quarantine_name: str, expected_identity: tuple[int, int], *,
        directory: bool,
    ) -> bool:
        """Remove one exact transaction quarantine after it has left the public namespace."""
        if QUARANTINE_RE.fullmatch(quarantine_name) is None:
            raise RuntimeError("Qwen recovery quarantine identity is unsafe")
        try:
            metadata = os.stat(
                quarantine_name, dir_fd=parent_fd, follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if directory:
            self._validate_index_lock(metadata)
        else:
            self._validate_snapshot_lock(metadata)
        if (metadata.st_dev, metadata.st_ino) != expected_identity:
            raise RuntimeError("Qwen recovery quarantine identity changed before purge")
        if directory:
            self._remove_tree_at(parent_fd, quarantine_name, expected_identity)
        else:
            descriptor = os.open(
                quarantine_name,
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                opened = os.fstat(descriptor)
                self._validate_snapshot_lock(opened)
                if (opened.st_dev, opened.st_ino) != expected_identity:
                    raise RuntimeError("Qwen recovery quarantine identity changed before purge")
                current = os.stat(
                    quarantine_name, dir_fd=parent_fd, follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != expected_identity:
                    raise RuntimeError("Qwen recovery quarantine identity changed before purge")
                os.unlink(quarantine_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(descriptor)
        return True

    def _purge_transaction_quarantine(
        self, transaction: dict[str, Any], *, receipt_prefix: str, parent_fd: int,
        expected_identity: tuple[int, int], directory: bool,
    ) -> bool:
        quarantine_name = transaction.get(f"{receipt_prefix}QuarantineName")
        if not isinstance(quarantine_name, str) \
                or QUARANTINE_RE.fullmatch(quarantine_name) is None:
            raise RuntimeError("Qwen recovery quarantine receipt is malformed")
        purged = self._purge_exact_quarantine(
            parent_fd,
            quarantine_name,
            expected_identity,
            directory=directory,
        )
        if transaction.get(f"{receipt_prefix}QuarantinePurged") not in {None, True}:
            raise RuntimeError("Qwen recovery quarantine purge state is not monotonic")
        transaction[f"{receipt_prefix}QuarantinePurged"] = True
        self.store.write(transaction)
        return purged

    def _probe_atomic_publication_capability(
        self, transaction: dict[str, Any], *, parent: Path, receipt_prefix: str,
    ) -> None:
        """Prove no-replace support before any cron or runtime mutation."""
        if not os.path.lexists(parent):
            raise RuntimeError("Atomic publication probe parent must already exist")
        stage_name = f".qwen-capability-probe-{uuid.uuid4().hex}"
        final_name = f".qwen-capability-probe-published-{uuid.uuid4().hex}"
        stage_fd: int | None = None
        identity: tuple[int, int] | None = None
        published = False
        cleanup_error: Exception | None = None
        with self._open_private_directory(parent) as parent_fd:
            try:
                os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
                stage_fd = os.open(
                    stage_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                metadata = os.fstat(stage_fd)
                self._validate_restricted_directory(metadata)
                identity = (metadata.st_dev, metadata.st_ino)
                transaction.update({
                    f"{receipt_prefix}Parent": str(Path(os.path.abspath(parent))),
                    f"{receipt_prefix}StageName": stage_name,
                    f"{receipt_prefix}StageDev": metadata.st_dev,
                    f"{receipt_prefix}StageIno": metadata.st_ino,
                    f"{receipt_prefix}FinalName": final_name,
                    f"{receipt_prefix}Published": False,
                })
                self.store.write(transaction)
                os.fsync(stage_fd)
                os.fsync(parent_fd)
                _rename_noreplace_at(parent_fd, stage_name, parent_fd, final_name)
                published = True
                os.fsync(parent_fd)
                final_meta = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
                self._validate_restricted_directory(final_meta)
                if (final_meta.st_dev, final_meta.st_ino) != identity:
                    raise RuntimeError("Atomic publication probe identity changed")
                transaction[f"{receipt_prefix}Published"] = True
                self.store.write(transaction)
                self._quarantine_transaction_path(
                    transaction,
                    receipt_prefix=receipt_prefix,
                    parent_fd=parent_fd,
                    name=final_name,
                    expected_identity=identity,
                    directory=True,
                )
                self._purge_transaction_quarantine(
                    transaction,
                    receipt_prefix=receipt_prefix,
                    parent_fd=parent_fd,
                    expected_identity=identity,
                    directory=True,
                )
            finally:
                if identity is not None and transaction.get(
                    f"{receipt_prefix}QuarantinePurged"
                ) is not True:
                    candidate = final_name if published else stage_name
                    try:
                        if transaction.get(f"{receipt_prefix}Quarantined") is not True:
                            self._quarantine_transaction_path(
                                transaction,
                                receipt_prefix=receipt_prefix,
                                parent_fd=parent_fd,
                                name=candidate,
                                expected_identity=identity,
                                directory=True,
                            )
                        self._purge_transaction_quarantine(
                            transaction,
                            receipt_prefix=receipt_prefix,
                            parent_fd=parent_fd,
                            expected_identity=identity,
                            directory=True,
                        )
                    except Exception as error:
                        cleanup_error = error
                        transaction[f"{receipt_prefix}Preserved"] = True
                        self.store.write(transaction)
                if stage_fd is not None:
                    try:
                        os.close(stage_fd)
                    except Exception as error:
                        if cleanup_error is None:
                            cleanup_error = error
                if cleanup_error is not None:
                    raise RuntimeError("Atomic publication capability probe cleanup was incomplete") \
                        from cleanup_error

    def _nearest_existing_capability_parent(self, target_parent: Path) -> Path:
        """Choose an existing ancestor on the filesystem that will host a fresh managed root."""
        target = Path(os.path.abspath(target_parent))
        home = Path(os.path.abspath(self.paths.home))
        if target != home and home not in target.parents:
            raise RuntimeError("Atomic publication probe parent is outside the managed home")
        _assert_no_symlink_components(target)
        candidate = target
        while not os.path.lexists(candidate):
            if candidate == home:
                raise RuntimeError("Managed home disappeared before atomic publication probe")
            candidate = candidate.parent
        if candidate != home and home not in candidate.parents:
            raise RuntimeError("Atomic publication probe ancestor escaped the managed home")
        return candidate

    def _capability_parent_from_transaction(
        self, transaction: dict[str, Any], *, receipt_prefix: str, target_parent: Path,
    ) -> Path:
        """Validate the receipted probe ancestor without recomputing it after paths are created."""
        stored = transaction.get(f"{receipt_prefix}Parent")
        target = Path(os.path.abspath(target_parent))
        if stored is None:
            return target
        if not isinstance(stored, str) or not stored:
            raise RuntimeError("Atomic publication capability parent receipt is malformed")
        raw = Path(stored).expanduser()
        if not raw.is_absolute():
            raise RuntimeError("Atomic publication capability parent receipt must be absolute")
        parent = Path(os.path.abspath(raw))
        home = Path(os.path.abspath(self.paths.home))
        if parent != home and home not in parent.parents:
            raise RuntimeError("Atomic publication capability parent escaped the managed home")
        if parent != target and parent not in target.parents:
            raise RuntimeError("Atomic publication capability parent is not an ancestor of its target")
        _assert_no_symlink_components(parent)
        return parent

    def _recover_capability_probe(
        self, transaction: dict[str, Any], *, receipt_prefix: str, expected_parent: Path,
    ) -> None:
        stage_name = transaction.get(f"{receipt_prefix}StageName")
        final_name = transaction.get(f"{receipt_prefix}FinalName")
        stage_dev = transaction.get(f"{receipt_prefix}StageDev")
        stage_ino = transaction.get(f"{receipt_prefix}StageIno")
        published = transaction.get(f"{receipt_prefix}Published")
        parent_value = transaction.get(f"{receipt_prefix}Parent")
        fields = (stage_name, final_name, stage_dev, stage_ino, published, parent_value)
        if all(value is None for value in fields):
            return
        if not isinstance(stage_name, str) \
                or CAPABILITY_PROBE_STAGE_RE.fullmatch(stage_name) is None \
                or not isinstance(final_name, str) \
                or CAPABILITY_PROBE_FINAL_RE.fullmatch(final_name) is None \
                or type(stage_dev) is not int or type(stage_ino) is not int \
                or type(published) is not bool or not isinstance(parent_value, str):
            raise RuntimeError("Atomic publication capability receipt is malformed")
        parent = Path(os.path.abspath(Path(parent_value).expanduser()))
        if parent != Path(os.path.abspath(expected_parent)):
            raise RuntimeError("Atomic publication capability parent changed")
        if not os.path.lexists(parent):
            transaction[f"{receipt_prefix}Preserved"] = False
            self.store.write(transaction)
            return
        expected = (stage_dev, stage_ino)
        with self._open_private_directory(parent) as parent_fd:
            matches: list[str] = []
            for name in (stage_name, final_name):
                try:
                    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                self._validate_restricted_directory(metadata)
                if (metadata.st_dev, metadata.st_ino) != expected:
                    raise RuntimeError("Atomic publication capability artifact changed")
                matches.append(name)
            quarantine_name = transaction.get(f"{receipt_prefix}QuarantineName")
            if quarantine_name is not None and (
                not isinstance(quarantine_name, str)
                or QUARANTINE_RE.fullmatch(quarantine_name) is None
            ):
                raise RuntimeError("Atomic publication capability quarantine receipt is malformed")
            if isinstance(quarantine_name, str):
                try:
                    metadata = os.stat(
                        quarantine_name, dir_fd=parent_fd, follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    self._validate_restricted_directory(metadata)
                    if (metadata.st_dev, metadata.st_ino) != expected:
                        raise RuntimeError("Atomic publication capability quarantine changed")
                    matches.append(quarantine_name)
            if len(matches) > 1:
                raise RuntimeError("Atomic publication capability artifact exists at multiple paths")
            if matches:
                candidate = matches[0]
                if candidate != quarantine_name:
                    self._quarantine_transaction_path(
                        transaction,
                        receipt_prefix=receipt_prefix,
                        parent_fd=parent_fd,
                        name=candidate,
                        expected_identity=expected,
                        directory=True,
                    )
                self._purge_transaction_quarantine(
                    transaction,
                    receipt_prefix=receipt_prefix,
                    parent_fd=parent_fd,
                    expected_identity=expected,
                    directory=True,
                )
                transaction[f"{receipt_prefix}Preserved"] = False
            else:
                transaction[f"{receipt_prefix}Preserved"] = False
            self.store.write(transaction)

    @contextmanager
    def _runtime_quiescence_guard(
        self, *, timeout_seconds: float = 1800, poll_seconds: float = 1.0,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Block new index/snapshot work while installer-owned runtime files are replaced."""
        if not self.paths.project_root.exists():
            yield {"snapshotLockCreated": False}
            return
        if checkpoint is None:
            raise RuntimeError("Runtime quiescence requires a durable transaction checkpoint")
        project = self.paths.project_root
        data = project / "data"
        _assert_no_symlink_components(data)
        data.mkdir(parents=True, exist_ok=True)
        data_meta = data.stat()
        if not stat.S_ISDIR(data_meta.st_mode) or data_meta.st_uid != os.getuid() \
                or data_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("Qwen data directory is unsafe for runtime quiescence")
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        data_fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        index_stage_name = f".index.lock.install-{uuid.uuid4().hex}"
        index_stage_fd: int | None = None
        index_identity: tuple[int, int] | None = None
        index_published = False
        snapshot_root_fd: int | None = None
        snapshot_stage_name: str | None = None
        snapshot_stage_fd: int | None = None
        snapshot_fd: int | None = None
        snapshot_locked = False
        snapshot_lock_created = False
        snapshot_published = False
        snapshot_identity: tuple[int, int] | None = None
        lock_receipt: dict[str, Any] = {
            "indexLockCreated": False,
            "snapshotLockCreated": False,
            "indexLockPersisted": False,
            "snapshotLockPersisted": False,
            "persisted": False,
        }
        try:
            os.mkdir(index_stage_name, 0o700, dir_fd=data_fd)
            index_stage_fd = os.open(
                index_stage_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=data_fd,
            )
            index_meta = os.fstat(index_stage_fd)
            self._validate_index_lock(index_meta)
            index_identity = (index_meta.st_dev, index_meta.st_ino)
            lock_receipt.update({
                "indexLockStageName": index_stage_name,
                "indexLockStageDev": index_meta.st_dev,
                "indexLockStageIno": index_meta.st_ino,
                "indexLockPublished": False,
            })
            checkpoint({
                "indexLockStageName": index_stage_name,
                "indexLockStageDev": index_meta.st_dev,
                "indexLockStageIno": index_meta.st_ino,
                "indexLockPublished": False,
            })
            lock_receipt["indexLockPersisted"] = True
            os.fsync(index_stage_fd)
            os.fsync(data_fd)
            while not index_published:
                try:
                    _rename_noreplace_at(data_fd, index_stage_name, data_fd, "index.lock")
                except FileExistsError:
                    self._validate_index_lock(os.stat(
                        "index.lock", dir_fd=data_fd, follow_symlinks=False,
                    ))
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Qwen index run did not quiesce before the bounded deadline")
                    time.sleep(min(max(0.01, poll_seconds), max(0.01, deadline - time.monotonic())))
                    continue
                published_meta = os.stat("index.lock", dir_fd=data_fd, follow_symlinks=False)
                self._validate_index_lock(published_meta)
                if (published_meta.st_dev, published_meta.st_ino) != index_identity:
                    raise RuntimeError("Published Qwen index lock identity changed")
                os.fsync(data_fd)
                lock_receipt.update({
                    "indexLockCreated": True,
                    "indexLockDev": published_meta.st_dev,
                    "indexLockIno": published_meta.st_ino,
                    "indexLockPublished": True,
                })
                checkpoint({
                    "indexLockCreated": True,
                    "indexLockDev": published_meta.st_dev,
                    "indexLockIno": published_meta.st_ino,
                    "indexLockPublished": True,
                })
                index_published = True

            snapshot_root_fd = os.open(
                self.snapshot_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                snapshot_fd = os.open(
                    ".snapshot-run.lock", os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=snapshot_root_fd,
                )
            except FileNotFoundError:
                snapshot_stage_name = f".snapshot-run.lock.install-{uuid.uuid4().hex}"
                snapshot_stage_fd = os.open(
                    snapshot_stage_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=snapshot_root_fd,
                )
                snapshot_stage_meta = os.fstat(snapshot_stage_fd)
                self._validate_snapshot_lock(snapshot_stage_meta)
                snapshot_identity = (snapshot_stage_meta.st_dev, snapshot_stage_meta.st_ino)
                lock_receipt.update({
                    "snapshotLockStageName": snapshot_stage_name,
                    "snapshotLockStageDev": snapshot_stage_meta.st_dev,
                    "snapshotLockStageIno": snapshot_stage_meta.st_ino,
                    "snapshotLockPublished": False,
                })
                checkpoint({
                    "snapshotLockStageName": snapshot_stage_name,
                    "snapshotLockStageDev": snapshot_stage_meta.st_dev,
                    "snapshotLockStageIno": snapshot_stage_meta.st_ino,
                    "snapshotLockPublished": False,
                })
                lock_receipt["snapshotLockPersisted"] = True
                os.fsync(snapshot_stage_fd)
                os.fsync(snapshot_root_fd)
                while snapshot_fd is None:
                    try:
                        _rename_noreplace_at(
                            snapshot_root_fd, snapshot_stage_name,
                            snapshot_root_fd, ".snapshot-run.lock",
                        )
                    except FileExistsError:
                        try:
                            existing_fd = os.open(
                                ".snapshot-run.lock", os.O_RDWR | os.O_NOFOLLOW,
                                dir_fd=snapshot_root_fd,
                            )
                        except FileNotFoundError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError(
                                    "Qwen snapshot lock namespace did not stabilize before the bounded deadline"
                                )
                            time.sleep(min(
                                max(0.01, poll_seconds),
                                max(0.01, deadline - time.monotonic()),
                            ))
                            continue
                        snapshot_fd = existing_fd
                        existing_meta = os.fstat(snapshot_fd)
                        self._validate_snapshot_lock(existing_meta)
                        self._quarantine_checkpointed_path(
                            lock_receipt,
                            receipt_prefix="snapshotLock",
                            parent_fd=snapshot_root_fd,
                            name=snapshot_stage_name,
                            expected_identity=snapshot_identity,
                            directory=False,
                            checkpoint=checkpoint,
                        )
                        os.close(snapshot_stage_fd)
                        snapshot_stage_fd = None
                        snapshot_identity = (existing_meta.st_dev, existing_meta.st_ino)
                        snapshot_lock_created = False
                    else:
                        os.fsync(snapshot_root_fd)
                        snapshot_fd = snapshot_stage_fd
                        snapshot_stage_fd = None
                        snapshot_lock_created = True
                        snapshot_published = True
                    break
            else:
                snapshot_lock_created = False
            snapshot_meta = os.fstat(snapshot_fd)
            self._validate_snapshot_lock(snapshot_meta)
            snapshot_identity = (snapshot_meta.st_dev, snapshot_meta.st_ino)
            lock_receipt.update({
                "snapshotLockCreated": snapshot_lock_created,
                "snapshotLockDev": snapshot_meta.st_dev,
                "snapshotLockIno": snapshot_meta.st_ino,
            })
            if snapshot_lock_created:
                lock_receipt["snapshotLockPublished"] = True
            checkpoint({
                "snapshotLockCreated": snapshot_lock_created,
                "snapshotLockDev": snapshot_meta.st_dev,
                "snapshotLockIno": snapshot_meta.st_ino,
                **({"snapshotLockPublished": True} if snapshot_lock_created else {}),
            })
            lock_receipt["snapshotLockPersisted"] = True
            while not snapshot_locked:
                try:
                    fcntl.flock(snapshot_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    snapshot_locked = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Qwen snapshot run did not quiesce before the bounded deadline")
                    time.sleep(min(max(0.01, poll_seconds), max(0.01, deadline - time.monotonic())))
            yield lock_receipt
        finally:
            cleanup_errors: list[Exception] = []

            def cleanup(action: Callable[[], Any]) -> None:
                try:
                    action()
                except Exception as error:
                    cleanup_errors.append(error)

            if snapshot_fd is not None:
                if snapshot_locked:
                    cleanup(lambda: fcntl.flock(snapshot_fd, fcntl.LOCK_UN))
                cleanup(lambda: os.close(snapshot_fd))
            if snapshot_root_fd is not None:
                if snapshot_stage_fd is not None and snapshot_stage_name is not None \
                        and snapshot_identity is not None:
                    cleanup(lambda: self._quarantine_checkpointed_path(
                        lock_receipt,
                        receipt_prefix="snapshotLock",
                        parent_fd=snapshot_root_fd,
                        name=snapshot_stage_name,
                        expected_identity=snapshot_identity,
                        directory=False,
                        checkpoint=checkpoint,
                    ))
                elif snapshot_published and snapshot_lock_created \
                        and lock_receipt.get("snapshotLockPersisted") is not True \
                        and snapshot_identity is not None:
                    cleanup(lambda: self._quarantine_checkpointed_path(
                        lock_receipt,
                        receipt_prefix="snapshotLock",
                        parent_fd=snapshot_root_fd,
                        name=".snapshot-run.lock",
                        expected_identity=snapshot_identity,
                        directory=False,
                        checkpoint=checkpoint,
                    ))
            if snapshot_stage_fd is not None:
                cleanup(lambda: os.close(snapshot_stage_fd))
            if snapshot_root_fd is not None:
                cleanup(lambda: os.close(snapshot_root_fd))
            if index_identity is not None:
                lock_name = "index.lock" if index_published else index_stage_name
                removed: list[bool] = []

                def quarantine_index() -> None:
                    removed.append(self._quarantine_checkpointed_path(
                        lock_receipt,
                        receipt_prefix="indexLock",
                        parent_fd=data_fd,
                        name=lock_name,
                        expected_identity=index_identity,
                        directory=True,
                        checkpoint=checkpoint,
                    ))

                cleanup(quarantine_index)
                if index_published and removed == [False]:
                    cleanup_errors.append(RuntimeError("Qwen index quiescence lock disappeared"))
            if index_stage_fd is not None:
                cleanup(lambda: os.close(index_stage_fd))
            cleanup(lambda: os.close(data_fd))
            if cleanup_errors:
                raise RuntimeError(
                    f"Qwen runtime quiescence cleanup was incomplete ({len(cleanup_errors)} errors)"
                ) from cleanup_errors[0]

    @staticmethod
    def _job_id_from_add(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise RuntimeError("OpenClaw cron add returned an unexpected schema")
        nested = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        outer_present = "id" in payload
        nested_present = "id" in nested
        outer_id = payload.get("id")
        nested_id = nested.get("id")
        if outer_present and nested_present and outer_id != nested_id:
            raise RuntimeError("OpenClaw cron add returned conflicting job ids")
        job_id = outer_id if outer_present else nested_id
        if not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id):
            raise RuntimeError("OpenClaw cron add did not return a job id")
        return job_id

    @staticmethod
    def _job_by_key(jobs: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        matches = [job for job in jobs if job.get("declarationKey") == key]
        if len(matches) > 1:
            raise RuntimeError("Managed cron declaration is duplicated")
        return matches[0] if matches else None

    def _legacy_incremental_job(self, job: dict[str, Any]) -> bool:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if not _known_cron_top_level_contract(job) or "toolsAllow" in payload:
            return False
        try:
            _job_definition(job)
        except RuntimeError:
            return False
        payload_keys = {
            "kind", "argv", "cwd", "timeoutSeconds", "noOutputTimeoutSeconds", "outputMaxBytes",
        }
        if set(payload) != payload_keys or payload.get("kind") != "command" \
                or _job_argv(job) != [str(self.paths.project_root / "scripts/knowledge_index_incremental.sh")] \
                or payload.get("cwd") != str(self.paths.project_root) \
                or payload.get("outputMaxBytes") != 65536:
            return False
        if job.get("declarationKey") != CRON_DECLARATION_KEY \
                or job.get("name") != "Qwen local knowledge incremental index" \
                or job.get("description") is not None \
                or not _default_cron_behavior_contract(job) \
                or type(job.get("enabled")) is not bool \
                or job.get("sessionTarget") != "isolated" \
                or job.get("sessionKey") is not None or job.get("agentId") is not None \
                or not (job.get("deleteAfterRun") is None or job.get("deleteAfterRun") is False) \
                or job.get("schedule") != {
                    "kind": "cron", "expr": "30 6 * * *", "tz": self.timezone_name, "staggerMs": 0,
                }:
            return False
        production_alert = {
            "after": 1,
            "channel": self.report_channel,
            "to": self.report_to,
            "cooldownMs": 3600000,
            "includeSkipped": False,
        }
        production_delivery = {
            "mode": "announce", "channel": self.report_channel, "to": self.report_to,
        }
        production_variant = (
            payload.get("timeoutSeconds") == 3600
            and payload.get("noOutputTimeoutSeconds") == 3600
            and job.get("delivery") == production_delivery
            and job.get("failureAlert") == production_alert
        )
        repository_variant = (
            payload.get("timeoutSeconds") == 7200
            and payload.get("noOutputTimeoutSeconds") == 900
            and _no_delivery_contract(job.get("delivery"))
            and job.get("failureAlert") in (None, {})
        )
        return production_variant or repository_variant

    def _legacy_snapshot_job_matches(self, job: dict[str, Any], *, require_known_key: bool) -> bool:
        if require_known_key and job.get("declarationKey") != LEGACY_SNAPSHOT_DECLARATION_KEY:
            return False
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        try:
            _job_definition(job)
        except RuntimeError:
            return False
        expected_argv = [
            "sh", "-lc", _legacy_snapshot_shell_command(
                project_root=self.paths.project_root,
                snapshot_root=self.snapshot_root,
                timezone_name=self.timezone_name,
            ),
        ]
        return (
            _known_cron_top_level_contract(job)
            and job.get("name") == LEGACY_SNAPSHOT_NAME
            and job.get("description") == LEGACY_SNAPSHOT_DESCRIPTION
            and _default_cron_behavior_contract(job)
            and type(job.get("enabled")) is bool
            and job.get("sessionTarget") == "isolated"
            and job.get("sessionKey") is None
            and job.get("agentId") is None
            and (job.get("deleteAfterRun") is None or job.get("deleteAfterRun") is False)
            and job.get("schedule") == {
                "kind": "cron", "expr": "50 6 * * *", "tz": self.timezone_name, "staggerMs": 0,
            }
            and payload == {
                "kind": "command",
                "argv": expected_argv,
                "cwd": str(self.paths.workspace),
                "noOutputTimeoutSeconds": 7200,
                "outputMaxBytes": 8192,
                "timeoutSeconds": 7200,
            }
            and job.get("delivery") == {
                "mode": "announce", "channel": self.report_channel, "to": self.report_to,
            }
            and job.get("failureAlert") == {
                "after": 1,
                "channel": self.report_channel,
                "to": self.report_to,
                "cooldownMs": 3600000,
                "includeSkipped": False,
            }
        )

    def _validate_existing_owned_jobs(self, jobs: list[dict[str, Any]]) -> None:
        for job in jobs:
            key = job.get("declarationKey")
            if key == CRON_DECLARATION_KEY:
                current = any(
                    _job_matches_spec(job, self._incremental_spec(), require_enabled=enabled)
                    for enabled in (True, False)
                )
                if not current and not self._legacy_incremental_job(job):
                    raise RuntimeError("Existing owned incremental cron is outside the safe upgrade allowlist")
            elif key == SNAPSHOT_CRON_DECLARATION_KEY:
                if not any(
                    _job_matches_spec(job, self._snapshot_spec(), require_enabled=enabled)
                    for enabled in (True, False)
                ):
                    raise RuntimeError("Existing owned snapshot cron is outside the safe upgrade allowlist")
            elif key == INITIAL_CRON_DECLARATION_KEY:
                if not any(self._initial_job_matches(job, enabled=enabled) for enabled in (True, False)):
                    raise RuntimeError("Existing owned initial cron is outside the safe upgrade allowlist")

    def _legacy_snapshot_candidates(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        exact_key = self._job_by_key(jobs, LEGACY_SNAPSHOT_DECLARATION_KEY)
        if exact_key is not None:
            if not self._legacy_snapshot_job_matches(exact_key, require_known_key=True):
                raise RuntimeError("Known legacy snapshot declaration is outside the exact migration allowlist")
            candidates.append(exact_key)
        if self.legacy_snapshot_job_id:
            supplied = [job for job in jobs if job.get("id") == self.legacy_snapshot_job_id]
            if len(supplied) != 1:
                raise RuntimeError("Operator-supplied legacy snapshot job id was not found exactly once")
            job = supplied[0]
            if not self._legacy_snapshot_job_matches(job, require_known_key=False):
                raise RuntimeError("Operator-supplied legacy snapshot job is outside the exact migration allowlist")
            if _job_contract_hash(job) != self.legacy_snapshot_job_sha256:
                raise RuntimeError("Operator-supplied legacy snapshot fingerprint does not match")
            if all(existing.get("id") != job.get("id") for existing in candidates):
                candidates.append(job)
        if len(candidates) > 1:
            raise RuntimeError("Legacy snapshot ownership is ambiguous")
        return candidates

    def _job_matches_approved_disabled_incremental_collision(
        self, job: dict[str, Any],
    ) -> bool:
        script = self.paths.project_root / "scripts/knowledge_index_incremental.sh"
        actual = _cron_contract_payload(job, include_id=False)
        if actual.get("declarationKey") == "":
            actual["declarationKey"] = None
        expected = {
            "name": "LanceDB 知識庫每日增量索引",
            "description": None,
            "enabled": False,
            "declarationKey": None,
            "schedule": {
                "kind": "cron", "expr": "30 6 * * *", "tz": self.timezone_name,
            },
            "payload": {
                "kind": "command",
                "argv": ["sh", "-lc", str(script)],
                "timeoutSeconds": 1800,
            },
            "delivery": {
                "mode": "announce", "channel": self.report_channel, "to": self.report_to,
            },
            "failureAlert": None,
            "sessionTarget": "isolated",
            "sessionKey": None,
            "agentId": None,
            "deleteAfterRun": False,
            "wakeMode": "now",
            "displayName": None,
            "owner": None,
            "trigger": None,
        }
        return (
            job.get("enabled") is False
            and _job_targets_exact_script(job, script)
            and actual == expected
        )

    def _approved_disabled_collision_job(
        self, jobs: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        approval = self.approved_disabled_collision
        if approval is None:
            return None
        matches = [job for job in jobs if job.get("id") == approval.job_id]
        if len(matches) != 1:
            raise RuntimeError(
                "Approved disabled collision job id was not found exactly once"
            )
        job = matches[0]
        if approval.role != "incremental" \
                or not self._job_matches_approved_disabled_incremental_collision(job):
            raise RuntimeError(
                "Approved disabled collision is outside the exact incremental contract"
            )
        if _job_contract_hash(job, include_id=True) != approval.contract_sha256:
            raise RuntimeError("Approved disabled collision fingerprint does not match")
        return job

    def _verify_approved_collision_receipt(
        self, transaction: dict[str, Any], jobs: list[dict[str, Any]],
    ) -> None:
        approval = self.approved_disabled_collision
        if approval is None:
            return
        ownership = transaction.get("ownership")
        if not isinstance(ownership, dict) \
                or ownership.get("approvedDisabledCollision") != approval.receipt():
            raise RuntimeError("Approved disabled collision ownership receipt drifted")
        unknown_hashes = transaction.get("cronUnknownHashesBefore")
        if not isinstance(unknown_hashes, dict) \
                or unknown_hashes.get(approval.job_id) != approval.contract_sha256:
            raise RuntimeError("Approved disabled collision unknown-inventory receipt drifted")
        current = self._approved_disabled_collision_job(jobs)
        if current is None or _job_contract_hash(current, include_id=True) != approval.contract_sha256:
            raise RuntimeError("Approved disabled collision readback drifted")

    def _preflight_cron_inventory(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        jobs = self._inventory()
        self._validate_existing_owned_jobs(jobs)
        incremental = self.paths.project_root / "scripts/knowledge_index_incremental.sh"
        snapshot_wrapper = self.paths.project_root / "scripts/run_verified_snapshot.py"
        full_index = self.paths.project_root / "scripts/knowledge_index_full.sh"
        approved = self._approved_disabled_collision_job(jobs)
        approved_id = str(approved["id"]) if approved is not None else None
        for job in jobs:
            key = job.get("declarationKey")
            if _job_targets_exact_script(job, incremental) and key != CRON_DECLARATION_KEY:
                if approved_id == str(job["id"]):
                    continue
                if self._job_matches_approved_disabled_incremental_collision(job):
                    raise RuntimeError(
                        "Disabled cron collision requires explicit approval: "
                        f"job id {job['id']}, role incremental, ID-inclusive SHA-256 "
                        f"{_job_contract_hash(job, include_id=True)}"
                    )
                raise RuntimeError("Unknown cron job targets the owned incremental wrapper")
            if _job_targets_snapshot_wrapper(job, snapshot_wrapper) and key != SNAPSHOT_CRON_DECLARATION_KEY:
                raise RuntimeError("Unknown cron job targets the owned snapshot wrapper")
            if _job_targets_exact_script(job, full_index) and key != INITIAL_CRON_DECLARATION_KEY:
                raise RuntimeError("Unknown cron job targets the owned initial index wrapper")
        legacy = self._legacy_snapshot_candidates(jobs)
        return jobs, legacy

    @staticmethod
    def _managed_pre_alert_definition(
        spec: ManagedCronSpec, staging_description: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "command",
            "argv": list(spec.argv),
            "cwd": spec.cwd,
            "timeoutSeconds": spec.timeout_seconds,
            "noOutputTimeoutSeconds": spec.no_output_timeout_seconds,
            "outputMaxBytes": spec.output_max_bytes,
        }
        if spec.command_env:
            payload["env"] = dict(spec.command_env)
        return {
            "name": spec.name,
            "description": staging_description,
            "enabled": False,
            "declarationKey": spec.key,
            "schedule": {
                "kind": "cron", "expr": spec.schedule, "tz": spec.timezone,
                "staggerMs": 0,
            },
            "payload": payload,
            "delivery": {"mode": "none"},
            "failureAlert": None,
            "sessionTarget": spec.session_target,
            "sessionKey": None,
            "agentId": None,
            "deleteAfterRun": False,
        }

    def _ensure_cron_intent(
        self,
        transaction: dict[str, Any],
        *,
        bucket_name: str,
        declaration_key: str,
        canonical_description: str,
        role: str,
        expected_factory: Callable[[str], dict[str, Any]],
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not declaration_key or not canonical_description:
            raise RuntimeError("Cron staging intent identity is incomplete")
        raw_bucket = transaction.setdefault(bucket_name, {})
        if not isinstance(raw_bucket, dict):
            raise RuntimeError("Cron staging intent receipt is malformed")
        intent = raw_bucket.get(declaration_key)
        if intent is None:
            staging_description = (
                f"{canonical_description} [qwen-stage:{uuid.uuid4()}]"
            )
            intent = {
                "schema": "qwen-local.cron-intent.v1",
                "declarationKey": declaration_key,
                "role": role,
                "stagingDescription": staging_description,
                "canonicalDescription": canonical_description,
                "preAlertContractSha256": _staging_contract_hash(
                    expected_factory(staging_description)
                ),
            }
            if extra_fields:
                intent.update(extra_fields)
            raw_bucket[declaration_key] = intent
            self.store.write(transaction)
        if not isinstance(intent, dict) or intent.get("schema") != "qwen-local.cron-intent.v1" \
                or intent.get("declarationKey") != declaration_key \
                or intent.get("role") != role \
                or intent.get("canonicalDescription") != canonical_description:
            raise RuntimeError("Cron staging intent receipt drifted")
        if extra_fields and any(intent.get(key) != value for key, value in extra_fields.items()):
            raise RuntimeError("Cron staging intent metadata drifted")
        staging_description = intent.get("stagingDescription")
        expected_hash = intent.get("preAlertContractSha256")
        if not isinstance(staging_description, str) or not staging_description \
                or not isinstance(expected_hash, str) \
                or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None \
                or _staging_contract_hash(expected_factory(staging_description)) != expected_hash:
            raise RuntimeError("Cron staging intent contract drifted")
        job_id = intent.get("jobId")
        if job_id is not None and (
            not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
        ):
            raise RuntimeError("Cron staging intent job id is malformed")
        return intent

    def _ensure_restore_cron_intent(
        self,
        transaction: dict[str, Any],
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a nonce name before restoring an exact prior cron definition."""
        declaration_key = definition.get("declarationKey")
        canonical_name = definition.get("name")
        canonical_description = definition.get("description")
        if not isinstance(declaration_key, str) or not declaration_key \
                or not isinstance(canonical_name, str) or not canonical_name \
                or canonical_description is not None \
                and (not isinstance(canonical_description, str) or not canonical_description):
            raise RuntimeError("Cron restore intent identity is incomplete")
        raw_bucket = transaction.setdefault("cronRestoreIntents", {})
        if not isinstance(raw_bucket, dict):
            raise RuntimeError("Cron restore intent receipt is malformed")
        intent = raw_bucket.get(declaration_key)
        if intent is None:
            staging_name = f"qwen-restore-stage-{uuid.uuid4()}"
            intent = {
                "schema": "qwen-local.cron-restore-intent.v1",
                "declarationKey": declaration_key,
                "role": "restore",
                "stagingName": staging_name,
                "canonicalName": canonical_name,
                "canonicalDescription": canonical_description,
                "preAlertContractSha256": _staging_contract_hash(
                    self._restore_pre_alert_definition(definition, staging_name)
                ),
            }
            raw_bucket[declaration_key] = intent
            self.store.write(transaction)
        if not isinstance(intent, dict) \
                or intent.get("schema") != "qwen-local.cron-restore-intent.v1" \
                or intent.get("declarationKey") != declaration_key \
                or intent.get("role") != "restore" \
                or intent.get("canonicalName") != canonical_name \
                or intent.get("canonicalDescription") != canonical_description:
            raise RuntimeError("Cron restore intent receipt drifted")
        staging_name = intent.get("stagingName")
        expected_hash = intent.get("preAlertContractSha256")
        if not isinstance(staging_name, str) or not staging_name \
                or not isinstance(expected_hash, str) \
                or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None \
                or _staging_contract_hash(
                    self._restore_pre_alert_definition(definition, staging_name)
                ) != expected_hash:
            raise RuntimeError("Cron restore intent contract drifted")
        job_id = intent.get("jobId")
        if job_id is not None and (
            not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
        ):
            raise RuntimeError("Cron restore intent job id is malformed")
        return intent

    def _checkpoint_cron_intent_job_id(
        self,
        transaction: dict[str, Any],
        *,
        bucket_name: str,
        declaration_key: str,
        job_id: str,
        created: bool,
    ) -> None:
        bucket = transaction.get(bucket_name)
        intent = bucket.get(declaration_key) if isinstance(bucket, dict) else None
        if not isinstance(intent, dict):
            raise RuntimeError("Cron staging intent is missing before id checkpoint")
        prior_id = intent.get("jobId")
        if prior_id not in (None, job_id):
            raise RuntimeError("Cron staging intent id changed")
        intent["jobId"] = job_id
        if created:
            ids = transaction.setdefault("managedCronIdsAfter", [])
            if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
                raise RuntimeError("Managed cron id receipt is malformed")
            if job_id not in ids:
                ids.append(job_id)
        else:
            restored = transaction.setdefault("restoredCronIdsByDeclaration", {})
            if not isinstance(restored, dict):
                raise RuntimeError("Restored cron id receipt is malformed")
            existing = restored.get(declaration_key)
            if existing not in (None, job_id):
                raise RuntimeError("Restored cron id receipt changed")
            restored[declaration_key] = job_id
        self.store.write(transaction)

    def _uncheckpointed_intent_candidate(
        self, intent: dict[str, Any], jobs: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        description = intent["stagingDescription"]
        declaration_key = intent["declarationKey"]
        description_matches = [job for job in jobs if job.get("description") == description]
        exact = [
            job for job in description_matches
            if job.get("declarationKey") == declaration_key
            and job.get("enabled", True) is False
            and _staging_contract_hash(job) == intent["preAlertContractSha256"]
        ]
        if len(description_matches) > 1 or len(exact) > 1:
            raise RuntimeError("Cron staging intent match is ambiguous")
        if description_matches and not exact:
            raise RuntimeError("Cron staging intent candidate contract drifted")
        return exact[0] if exact else None

    def _uncheckpointed_restore_intent_candidate(
        self, intent: dict[str, Any], jobs: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        staging_name = intent["stagingName"]
        declaration_key = intent["declarationKey"]
        name_matches = [job for job in jobs if job.get("name") == staging_name]
        exact = [
            job for job in name_matches
            if job.get("declarationKey") == declaration_key
            and job.get("enabled", True) is False
            and _staging_contract_hash(job) == intent["preAlertContractSha256"]
        ]
        if len(name_matches) > 1 or len(exact) > 1:
            raise RuntimeError("Cron restore intent match is ambiguous")
        if name_matches and not exact:
            raise RuntimeError("Cron restore intent candidate contract drifted")
        return exact[0] if exact else None

    def _validate_intent_candidate_authority(
        self,
        transaction: dict[str, Any],
        intent: dict[str, Any],
        candidate: dict[str, Any],
        *,
        returned_id: str | None = None,
        jobs_before_add: list[dict[str, Any]] | None = None,
        jobs_after_add: list[dict[str, Any]] | None = None,
    ) -> str:
        """Prove a nonce-staged job identity before making its ID destructive authority."""
        candidate_id = str(candidate.get("id") or "")
        if not SAFE_CRON_JOB_ID_RE.fullmatch(candidate_id):
            raise RuntimeError("Cron staging intent candidate id is malformed")
        persisted = self.store.read()
        bucket_name = (
            "cronRestoreIntents" if intent.get("role") == "restore"
            else "cronStagingIntents"
        )
        bucket = persisted.get(bucket_name)
        persisted_intent = bucket.get(intent.get("declarationKey")) \
            if isinstance(bucket, dict) else None
        if persisted_intent != intent:
            raise RuntimeError("Cron staging intent is not durably identical")
        inventory_before = persisted.get("cronInventoryHashesBefore")
        if not isinstance(inventory_before, dict) or any(
            not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            for job_id, fingerprint in inventory_before.items()
        ):
            raise RuntimeError("Cron staging inventory authority is malformed")
        if candidate_id in inventory_before:
            raise RuntimeError("Cron staging candidate reuses a preflight job id")
        if returned_id is not None:
            if not SAFE_CRON_JOB_ID_RE.fullmatch(returned_id):
                raise RuntimeError("Cron add returned an unsafe job id")
            if jobs_before_add is None or jobs_after_add is None:
                raise RuntimeError("Cron add identity validation lacks its pre-add inventory")
            hashes_before_add = self._inventory_hashes(jobs_before_add)
            hashes_after_add = self._inventory_hashes(jobs_after_add)
            if returned_id in hashes_before_add:
                raise RuntimeError("Cron add returned a pre-existing job id")
            if returned_id != candidate_id:
                raise RuntimeError("Cron add returned an id outside its unique staging intent")
            if set(hashes_after_add) != set(hashes_before_add) | {candidate_id} \
                    or any(
                        hashes_after_add.get(job_id) != fingerprint
                        for job_id, fingerprint in hashes_before_add.items()
                    ) \
                    or hashes_after_add.get(candidate_id) != _job_contract_hash(
                        candidate, include_id=True,
                    ):
                raise RuntimeError("Cron add changed more than its exact staged candidate")
        return candidate_id

    @staticmethod
    def _validated_cron_intent_receipts(
        transaction: dict[str, Any], bucket_name: str,
    ) -> dict[str, dict[str, Any]]:
        raw = transaction.get(bucket_name, {})
        if not isinstance(raw, dict):
            raise RuntimeError("Cron intent receipt bucket is malformed")
        validated: dict[str, dict[str, Any]] = {}
        staging_identities: set[str] = set()
        for declaration_key, intent in raw.items():
            if not isinstance(declaration_key, str) or not declaration_key \
                    or not isinstance(intent, dict) \
                    or intent.get("declarationKey") != declaration_key \
                    or intent.get("role") not in {"managed", "initial", "restore"}:
                raise RuntimeError("Cron intent identity receipt is malformed")
            if bucket_name == "cronStagingIntents" and (
                declaration_key not in MANAGED_CRON_KEYS
                or intent.get("role") == "initial"
                and declaration_key != INITIAL_CRON_DECLARATION_KEY
                or intent.get("role") == "managed"
                and declaration_key == INITIAL_CRON_DECLARATION_KEY
                or intent.get("role") == "restore"
            ):
                raise RuntimeError("Cron staging intent is outside managed declarations")
            if bucket_name == "cronRestoreIntents" and intent.get("role") != "restore":
                raise RuntimeError("Cron restore intent role is invalid")
            fingerprint = intent.get("preAlertContractSha256")
            if not isinstance(fingerprint, str) \
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
                raise RuntimeError("Cron intent contract receipt is malformed")
            if bucket_name == "cronRestoreIntents":
                identity = intent.get("stagingName")
                canonical_name = intent.get("canonicalName")
                canonical_description = intent.get("canonicalDescription")
                if intent.get("schema") != "qwen-local.cron-restore-intent.v1" \
                        or not isinstance(identity, str) or not identity \
                        or not isinstance(canonical_name, str) or not canonical_name \
                        or canonical_description is not None and (
                            not isinstance(canonical_description, str)
                            or not canonical_description
                        ):
                    raise RuntimeError("Cron restore intent contract receipt is malformed")
            else:
                identity = intent.get("stagingDescription")
                canonical_description = intent.get("canonicalDescription")
                if intent.get("schema") != "qwen-local.cron-intent.v1" \
                        or not isinstance(identity, str) or not identity \
                        or not isinstance(canonical_description, str) \
                        or not canonical_description:
                    raise RuntimeError("Cron intent contract receipt is malformed")
            if identity in staging_identities:
                raise RuntimeError("Cron intent staging identity is duplicated")
            staging_identities.add(identity)
            job_id = intent.get("jobId")
            if job_id is not None and (
                not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
            ):
                raise RuntimeError("Cron intent job id receipt is malformed")
            validated[declaration_key] = intent
        return validated

    def _managed_intent_lifecycle_contracts(
        self, intent: dict[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        """Recompute every installer-created state authorized by one durable intent."""
        declaration_key = intent.get("declarationKey")
        role = intent.get("role")
        staging_description = intent.get("stagingDescription")
        canonical_description = intent.get("canonicalDescription")
        allowed_fields = {
            "schema", "declarationKey", "role", "stagingDescription",
            "canonicalDescription", "preAlertContractSha256", "jobId", "configured",
        }
        if role == "initial":
            allowed_fields.add("scheduledAt")
        if set(intent) - allowed_fields or (
            "configured" in intent and intent.get("configured") is not True
        ):
            raise RuntimeError("Cron staging intent receipt has unsupported fields")
        if not isinstance(staging_description, str) \
                or not isinstance(canonical_description, str) \
                or re.fullmatch(
                    re.escape(canonical_description)
                    + r" \[qwen-stage:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\]",
                    staging_description,
                ) is None:
            raise RuntimeError("Cron staging intent nonce is malformed")

        if role == "managed" and declaration_key in {
            CRON_DECLARATION_KEY, SNAPSHOT_CRON_DECLARATION_KEY,
        }:
            spec = (
                self._incremental_spec()
                if declaration_key == CRON_DECLARATION_KEY
                else self._snapshot_spec()
            )
            if canonical_description != spec.description:
                raise RuntimeError("Cron staging intent canonical contract drifted")
            staged = self._managed_pre_alert_definition(spec, staging_description)
            alert_spec = spec
        elif role == "initial" and declaration_key == INITIAL_CRON_DECLARATION_KEY:
            scheduled_at = intent.get("scheduledAt")
            if not isinstance(scheduled_at, str) or not scheduled_at:
                raise RuntimeError("Initial cron staging intent schedule is malformed")
            try:
                parsed_at = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise RuntimeError("Initial cron staging intent schedule is malformed") from error
            canonical_at = parsed_at.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z") if parsed_at.tzinfo is not None else None
            if canonical_at != scheduled_at \
                    or canonical_description != INITIAL_CRON_DESCRIPTION:
                raise RuntimeError("Initial cron staging intent canonical contract drifted")
            staged = self._initial_pre_alert_definition(staging_description, scheduled_at)
            alert_spec = self._incremental_spec()
        else:
            raise RuntimeError("Cron staging intent is outside installer-managed roles")

        if _staging_contract_hash(staged) != intent.get("preAlertContractSha256"):
            raise RuntimeError("Cron staging intent semantic receipt drifted")
        alert = {
            "after": 1,
            "cooldownMs": 3600000,
            "includeSkipped": False,
            "mode": "announce",
            "channel": alert_spec.report_channel,
            "to": alert_spec.report_to,
            "accountId": alert_spec.report_account_id,
        }
        staged_alerted = json.loads(json.dumps(staged))
        staged_alerted["failureAlert"] = alert
        configured_disabled = json.loads(json.dumps(staged_alerted))
        configured_disabled["description"] = canonical_description
        configured_enabled = json.loads(json.dumps(configured_disabled))
        configured_enabled["enabled"] = True
        return staged, staged_alerted, configured_disabled, configured_enabled

    def _managed_intent_lifecycle_matches(
        self, job: dict[str, Any], intent: dict[str, Any],
    ) -> bool:
        actual_hash = _staging_contract_hash(job)
        return any(
            actual_hash == _staging_contract_hash(candidate)
            for candidate in self._managed_intent_lifecycle_contracts(intent)
        )

    def _activation_fail_safe_plan(
        self, transaction: dict[str, Any],
    ) -> dict[str, tuple[dict[str, Any], str, str]]:
        """Validate durable exact-ID authority for an interrupted activation."""
        if transaction.get("contractVersion") != INTEGRATION_CONTRACT_VERSION \
                or transaction.get("ownership") != self._ownership_payload() \
                or transaction.get("phase") not in {
                    "activation_pending", "commit_closeout_pending", "failed",
                    "rollback_failed", "committed",
                }:
            raise RuntimeError("Activation fail-safe transaction authority is invalid")
        if transaction.get("activationFailSafeRequired") is not True:
            raise RuntimeError("Activation fail-safe is not durably armed")
        self._validate_replacement_receipt_graph(transaction)
        intents = self._validated_cron_intent_receipts(
            transaction, "cronStagingIntents",
        )
        expected_by_key: dict[str, str | None] = {
            CRON_DECLARATION_KEY: transaction.get("cronId"),
            SNAPSHOT_CRON_DECLARATION_KEY: transaction.get("snapshotCronId"),
        }
        index_state = transaction.get("indexState")
        if index_state == "INDEX_BUILDING":
            expected_by_key[INITIAL_CRON_DECLARATION_KEY] = transaction.get(
                "initialIndexJobId"
            )
        elif index_state == "READY":
            if transaction.get("initialIndexJobId") is not None:
                raise RuntimeError("Activation fail-safe READY receipt has an initial job")
        else:
            raise RuntimeError("Activation fail-safe index state is invalid")
        if set(intents) != set(expected_by_key):
            raise RuntimeError("Activation fail-safe intent topology is incomplete")

        plan: dict[str, tuple[dict[str, Any], str, str]] = {}
        for declaration_key, raw_job_id in expected_by_key.items():
            if not isinstance(raw_job_id, str) \
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(raw_job_id):
                raise RuntimeError("Activation fail-safe job id receipt is malformed")
            intent = intents[declaration_key]
            if intent.get("jobId") != raw_job_id or intent.get("configured") is not True:
                raise RuntimeError("Activation fail-safe intent id receipt is incomplete")
            lifecycle = self._managed_intent_lifecycle_contracts(intent)
            if raw_job_id in plan:
                raise RuntimeError("Activation fail-safe job id authority overlaps")
            plan[raw_job_id] = (
                intent,
                _staging_contract_hash(lifecycle[2]),
                _staging_contract_hash(lifecycle[3]),
            )

        managed_ids = transaction.get("managedCronIdsAfter")
        if not isinstance(managed_ids, list) \
                or any(
                    not isinstance(job_id, str)
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    for job_id in managed_ids
                ) \
                or len(managed_ids) != len(set(managed_ids)) \
                or set(managed_ids) != set(plan):
            raise RuntimeError("Activation fail-safe managed id receipt is inconsistent")
        disabled_ids = transaction.get("activationFailSafeDisabledCronIds", [])
        if not isinstance(disabled_ids, list) \
                or any(
                    not isinstance(job_id, str)
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    for job_id in disabled_ids
                ) \
                or len(disabled_ids) != len(set(disabled_ids)) \
                or not set(disabled_ids).issubset(plan):
            raise RuntimeError("Activation fail-safe progress receipt is malformed")
        if type(transaction.get("activationFailSafeComplete", False)) is not bool:
            raise RuntimeError("Activation fail-safe completion receipt is malformed")
        return plan

    def _disable_uncommitted_managed_jobs_for_activation_failure(
        self, transaction: dict[str, Any],
    ) -> None:
        """Disable every durably checkpointed new job before strict rollback checks."""
        plan = self._activation_fail_safe_plan(transaction)
        errors: list[Exception] = []
        progress = json.loads(json.dumps(transaction))
        progress["activationFailSafeStarted"] = True
        progress["activationFailSafeComplete"] = False
        try:
            self.store.write(progress)
        except Exception as error:
            # The already-durable armed receipt remains exact disable authority.
            # A progress-marker failure must not leave later managed jobs enabled.
            errors.append(error)
        for job_id in sorted(plan):
            try:
                persisted = self.store.read()
                persisted_plan = self._activation_fail_safe_plan(persisted)
                if persisted_plan != plan:
                    raise RuntimeError("Activation fail-safe durable authority drifted")
                current_jobs = self._inventory()
                current = next(
                    (job for job in current_jobs if str(job["id"]) == job_id), None,
                )
                if current is None:
                    raise RuntimeError("Activation fail-safe target disappeared")
                _, disabled_hash, enabled_hash = plan[job_id]
                current_hash = _staging_contract_hash(current)
                if current_hash not in {disabled_hash, enabled_hash}:
                    raise RuntimeError("Activation fail-safe target lifecycle drifted")
                if current_hash == enabled_hash:
                    self._edit_cron_with_snapshot_guard(
                        job_id,
                        ["cron", "edit", job_id, "--disable"],
                        before=lambda job, expected=enabled_hash: _staging_contract_hash(
                            job
                        ) == expected,
                        after=lambda job, expected=disabled_hash: _staging_contract_hash(
                            job
                        ) == expected,
                        label="Activation failure compensation",
                    )
                verified = next(
                    (job for job in self._inventory() if str(job["id"]) == job_id),
                    None,
                )
                if verified is None or _staging_contract_hash(verified) != disabled_hash:
                    raise RuntimeError("Activation fail-safe target did not remain disabled")
                if self._runtime_job_active(verified):
                    raise RuntimeError("Activation fail-safe target remained active")
                disabled_ids = persisted.setdefault(
                    "activationFailSafeDisabledCronIds", []
                )
                if job_id not in disabled_ids:
                    disabled_ids.append(job_id)
                    disabled_ids.sort()
                persisted["activationFailSafeStarted"] = True
                persisted["activationFailSafeComplete"] = False
                self.store.write(persisted)
            except Exception as error:
                errors.append(error)

        try:
            self._wait_for_quiesced_jobs(set(plan))
        except Exception as error:
            errors.append(error)
        final_jobs = self._inventory()
        final_by_id = {str(job["id"]): job for job in final_jobs}
        completed = self.store.read()
        for job_id, (_, disabled_hash, _) in plan.items():
            job = final_by_id.get(job_id)
            try:
                if job is None:
                    raise RuntimeError("Activation fail-safe target is missing at closeout")
                if _staging_contract_hash(job) != disabled_hash \
                        or self._runtime_job_active(job):
                    raise RuntimeError(
                        "Activation fail-safe did not quiesce every managed job"
                    )
            except Exception as error:
                errors.append(error)
        try:
            if self._activation_fail_safe_plan(completed) != plan:
                raise RuntimeError(
                    "Activation fail-safe durable authority drifted at closeout"
                )
        except Exception as error:
            errors.append(error)
        safe_ids: set[str] = set()
        for job_id, (_, disabled_hash, _) in plan.items():
            job = final_by_id.get(job_id)
            if job is None:
                continue
            try:
                if _staging_contract_hash(job) == disabled_hash \
                        and not self._runtime_job_active(job):
                    safe_ids.add(job_id)
            except Exception:
                continue
        disabled_receipt = completed.setdefault(
            "activationFailSafeDisabledCronIds", []
        )
        for job_id in sorted(safe_ids):
            if job_id not in disabled_receipt:
                disabled_receipt.append(job_id)
        disabled_receipt.sort()
        completed["activationFailSafeStarted"] = True
        completed["activationFailSafeComplete"] = not errors \
            and set(disabled_receipt) == set(plan)
        self.store.write(completed)
        transaction.clear()
        transaction.update(completed)
        if errors:
            raise RuntimeError(
                f"Activation fail-safe compensation was incomplete ({len(errors)} errors)"
            ) from errors[0]

    def _validate_replacement_receipt_graph(
        self, transaction: dict[str, Any],
    ) -> bool:
        """Cross-bind phase-sensitive replacement receipts for new transactions."""
        raw_phase = transaction.get("phase")
        if raw_phase in {"failed", "rollback_failed"}:
            effective_phase = transaction.get("failurePhase")
        elif raw_phase == "rolled_back":
            effective_phase = transaction.get("rollbackOriginPhase")
        else:
            effective_phase = raw_phase
        pre_replace_phases = {
            "prepared", "preflight_complete", "quiescing", "quiesced",
            "staging", "activating",
        }
        post_replace_phases = {
            "staging_managed_cron", "restarting_gateway", "activation_pending",
            "commit_closeout_pending", "committed",
        }
        recognized_phases = pre_replace_phases | {
            "replacing_managed_cron",
        } | post_replace_phases
        new_only_phases = {
            "replacing_managed_cron", "staging_managed_cron",
            "restarting_gateway", "activation_pending", "commit_closeout_pending",
        }
        receipt_fields = {
            "cronInventoryTotalBefore", "disabledGeminiJobs",
            "cronReplaceIdsBefore", "removedManagedCronIdsBeforeAdd",
        }
        if not (receipt_fields & set(transaction)):
            committed_new_format = raw_phase == "committed" and (
                transaction.get("cronContractHashVersion")
                == CRON_CONTRACT_HASH_VERSION
                or any(key.startswith("cronCommit") for key in transaction)
            )
            if effective_phase in new_only_phases \
                    or transaction.get("activationFailSafeRequired") is True \
                    or committed_new_format:
                raise RuntimeError(
                    "Cron replacement receipt graph is missing for its effective phase"
                )
            return False
        if raw_phase in {"failed", "rollback_failed", "rolled_back"}:
            if not isinstance(effective_phase, str) \
                    or effective_phase not in recognized_phases:
                raise RuntimeError(
                    "Cron replacement terminal phase authority is missing or unknown"
                )
        elif raw_phase not in recognized_phases:
            raise RuntimeError("Cron replacement transaction phase is unknown")
        inventory_before = transaction.get("cronInventoryHashesBefore")
        unknown_before = transaction.get("cronUnknownHashesBefore")
        target_ids = transaction.get("cronTargetIdsBefore")
        raw_definitions = transaction.get("cronDefinitionsBefore")
        total_before = transaction.get("cronInventoryTotalBefore")
        disabled_gemini = transaction.get("disabledGeminiJobs")
        preserved_gemini_hashes = transaction.get(
            "cronPreservedGeminiHashesAfterQuiesce"
        )
        if not isinstance(inventory_before, dict) \
                or any(
                    not isinstance(job_id, str)
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    for job_id, fingerprint in inventory_before.items()
                ) \
                or not isinstance(unknown_before, dict) \
                or any(
                    not isinstance(job_id, str)
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    for job_id, fingerprint in unknown_before.items()
                ) \
                or not isinstance(target_ids, list) \
                or any(
                    not isinstance(job_id, str)
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    for job_id in target_ids
                ) \
                or len(target_ids) != len(set(target_ids)) \
                or type(total_before) is not int \
                or total_before != len(inventory_before) \
                or not isinstance(raw_definitions, list) \
                or any(not isinstance(item, dict) for item in raw_definitions) \
                or not isinstance(disabled_gemini, list) \
                or not isinstance(preserved_gemini_hashes, dict) \
                or any(
                    not isinstance(job_id, str)
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    for job_id, fingerprint in preserved_gemini_hashes.items()
                ):
            raise RuntimeError("Cron replacement receipt graph is malformed")
        definitions = [_job_definition(item) for item in raw_definitions]
        definitions_by_id = {str(item["id"]): item for item in definitions}
        if len(definitions_by_id) != len(definitions):
            raise RuntimeError("Cron replacement definition ids are duplicated")
        target_set = set(target_ids)
        unknown_set = set(unknown_before)
        if target_set != set(definitions_by_id) \
                or target_set & unknown_set \
                or set(inventory_before) != target_set | unknown_set \
                or any(
                    inventory_before.get(job_id) != fingerprint
                    for job_id, fingerprint in unknown_before.items()
                ):
            raise RuntimeError("Cron replacement ownership partition is inconsistent")
        for job_id, definition in definitions_by_id.items():
            if inventory_before.get(job_id) != _job_contract_hash(
                definition, include_id=True,
            ):
                raise RuntimeError(
                    "Cron replacement definition fingerprint is inconsistent"
                )
        expected_replace_ids = sorted(
            job_id for job_id, definition in definitions_by_id.items()
            if definition.get("declarationKey") in MANAGED_CRON_KEYS
        )
        expected_disabled_gemini = sorted(
            (
                {"id": job_id, "wasEnabled": True}
                for job_id, definition in definitions_by_id.items()
                if definition.get("declarationKey") == GEMINI_DECLARATION_KEY
                and definition.get("enabled") is True
            ),
            key=lambda item: item["id"],
        )
        expected_preserved_gemini_hashes: dict[str, str] = {}
        for job_id, definition in definitions_by_id.items():
            if definition.get("declarationKey") != GEMINI_DECLARATION_KEY:
                continue
            expected_disabled = json.loads(json.dumps(definition))
            expected_disabled["enabled"] = False
            expected_preserved_gemini_hashes[job_id] = _job_contract_hash(
                expected_disabled, include_id=True,
            )
        if preserved_gemini_hashes != expected_preserved_gemini_hashes:
            raise RuntimeError(
                "Cron replacement preserved Gemini receipt is inconsistent"
            )
        if any(
            not isinstance(item, dict)
            or set(item) != {"id", "wasEnabled"}
            or not isinstance(item.get("id"), str)
            or not SAFE_CRON_JOB_ID_RE.fullmatch(item["id"])
            or item.get("wasEnabled") is not True
            for item in disabled_gemini
        ) \
                or sorted(disabled_gemini, key=lambda item: item["id"]) \
                != expected_disabled_gemini:
            raise RuntimeError("Cron replacement Gemini disable receipt is inconsistent")

        replace_ids = transaction.get("cronReplaceIdsBefore")
        removed_ids = transaction.get("removedManagedCronIdsBeforeAdd")
        for key, value, label in (
            ("cronReplaceIdsBefore", replace_ids, "replace"),
            ("removedManagedCronIdsBeforeAdd", removed_ids, "removed"),
        ):
            if key in transaction and (
                not isinstance(value, list)
                or any(
                    not isinstance(job_id, str)
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    for job_id in value
                )
                or len(value) != len(set(value))
                or value != expected_replace_ids
            ):
                raise RuntimeError(
                    f"Cron replacement {label} id receipt is inconsistent"
                )

        replace_present = "cronReplaceIdsBefore" in transaction
        removed_present = "removedManagedCronIdsBeforeAdd" in transaction
        if effective_phase in pre_replace_phases and (
            replace_present or removed_present
        ):
            raise RuntimeError("Cron replacement receipts precede their authorized phase")
        if effective_phase == "replacing_managed_cron" and (
            replace_ids != expected_replace_ids or removed_present
        ):
            raise RuntimeError(
                "Cron replacement durable ids or phase boundary are inconsistent"
            )
        if effective_phase in post_replace_phases and (
            not replace_present or not removed_present
            or replace_ids != expected_replace_ids
            or removed_ids != expected_replace_ids
        ):
            raise RuntimeError("Cron replacement completion receipts are missing")
        return True

    def _apply_managed_spec(
        self,
        spec: ManagedCronSpec,
        transaction: dict[str, Any],
        *,
        enable: bool = False,
    ) -> str:
        intent = self._ensure_cron_intent(
            transaction,
            bucket_name="cronStagingIntents",
            declaration_key=spec.key,
            canonical_description=spec.description,
            role="managed",
            expected_factory=lambda description: self._managed_pre_alert_definition(
                spec, description,
            ),
        )
        if intent.get("jobId") is not None:
            raise RuntimeError(f"Managed cron {spec.key} staging id already exists")
        jobs = self._inventory()
        candidate = self._uncheckpointed_intent_candidate(intent, jobs)
        if candidate is not None:
            job_id = self._validate_intent_candidate_authority(
                transaction, intent, candidate,
            )
        else:
            if self._job_by_key(jobs, spec.key) is not None:
                raise RuntimeError(f"Managed cron {spec.key} appeared before authorized add")
            returned_id = self._job_id_from_add(self.cli.json(spec.add_args(
                disabled=True, description=intent["stagingDescription"],
            )))
            after_add = self._inventory()
            candidate = self._uncheckpointed_intent_candidate(intent, after_add)
            if candidate is None:
                raise RuntimeError(f"Managed cron {spec.key} add lacked an exact staging job")
            job_id = self._validate_intent_candidate_authority(
                transaction, intent, candidate,
                returned_id=returned_id, jobs_before_add=jobs,
                jobs_after_add=after_add,
            )
        self._checkpoint_cron_intent_job_id(
            transaction,
            bucket_name="cronStagingIntents",
            declaration_key=spec.key,
            job_id=job_id,
            created=True,
        )
        staged = next((job for job in self._inventory() if str(job["id"]) == job_id), None)
        if staged is None or _staging_contract_hash(staged) != intent["preAlertContractSha256"]:
            raise RuntimeError(f"Managed cron {spec.key} failed staged readback verification")
        lifecycle = self._managed_intent_lifecycle_contracts(intent)
        self._edit_cron_with_snapshot_guard(
            job_id,
            spec.alert_args(job_id),
            before=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                lifecycle[0]
            ),
            after=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                lifecycle[1]
            ),
            label=f"Managed {spec.key} alert",
        )
        self._edit_cron_with_snapshot_guard(
            job_id,
            ["cron", "edit", job_id, "--description", spec.description, "--disable"],
            before=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                lifecycle[1]
            ),
            after=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                lifecycle[2]
            ),
            label=f"Managed {spec.key} description",
        )
        disabled = self._job_by_key(self._inventory(), spec.key)
        if disabled is None or disabled.get("id") != job_id or not _job_matches_spec(
            disabled, spec, require_enabled=False
        ):
            raise RuntimeError(f"Managed cron {spec.key} failed disabled readback verification")
        if enable:
            self._edit_cron_with_snapshot_guard(
                job_id,
                ["cron", "edit", job_id, "--enable"],
                before=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                    lifecycle[2]
                ),
                after=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                    lifecycle[3]
                ),
                label=f"Managed {spec.key} activation",
            )
            enabled = self._job_by_key(self._inventory(), spec.key)
            if enabled is None or enabled.get("id") != job_id or not _job_matches_spec(
                enabled, spec, require_enabled=True
            ):
                raise RuntimeError(f"Managed cron {spec.key} failed enabled readback verification")
        intent["configured"] = True
        self.store.write(transaction)
        return job_id

    def _verify_recurring_specs(self, *, enabled: bool) -> list[dict[str, Any]]:
        jobs = self._inventory()
        for spec in (self._incremental_spec(), self._snapshot_spec()):
            job = self._job_by_key(jobs, spec.key)
            if job is None or not _job_matches_spec(job, spec, require_enabled=enabled):
                state = "enabled" if enabled else "disabled"
                raise RuntimeError(f"Managed cron {spec.key} failed global {state} verification")
        return jobs

    def _enable_recurring_jobs(self, job_ids: list[str]) -> None:
        if len(job_ids) != 2 or len(set(job_ids)) != 2:
            raise RuntimeError("Recurring cron activation set is incomplete")
        for job_id, spec in zip(
            job_ids, (self._incremental_spec(), self._snapshot_spec()),
        ):
            self._edit_cron_with_snapshot_guard(
                job_id,
                ["cron", "edit", job_id, "--enable"],
                before=lambda job, expected=spec: _job_matches_spec(
                    job, expected, require_enabled=False,
                ),
                after=lambda job, expected=spec: _job_matches_spec(
                    job, expected, require_enabled=True,
                ),
                label=f"Recurring {spec.key} activation",
            )
        self._verify_recurring_specs(enabled=True)

    def create_incremental_cron(self) -> str:
        """Compatibility lookup that never creates cron outside a committed transaction."""
        transaction = self.store.read()
        if transaction.get("contractVersion") != INTEGRATION_CONTRACT_VERSION \
                or transaction.get("phase") != "committed":
            raise RuntimeError(
                "Incremental cron creation requires the transactional integration workflow"
            )
        self.verify()
        job_id = transaction.get("cronId")
        if not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id):
            raise RuntimeError("Committed incremental cron id is malformed")
        return job_id

    def disable_owned_gemini_jobs(self) -> list[dict[str, Any]]:
        jobs = self._inventory()
        disabled = []
        for job in self._owned_gemini_jobs_exact(jobs):
            if job.get("enabled", True):
                job_id = str(job["id"])
                expected_disabled = json.loads(json.dumps(job))
                expected_disabled["enabled"] = False
                self._edit_cron_with_snapshot_guard(
                    job_id,
                    ["cron", "edit", job_id, "--disable"],
                    before=lambda current, expected=job: _job_contract_hash(
                        current, include_id=True,
                    ) == _job_contract_hash(expected, include_id=True),
                    after=lambda current, expected=expected_disabled: _job_contract_hash(
                        current, include_id=True,
                    ) == _job_contract_hash(expected, include_id=True),
                    label="Gemini quiescence",
                )
                disabled.append({"id": job_id, "wasEnabled": True})
        return disabled

    def begin(
        self, *, cron_inventory_hashes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
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
        if cron_inventory_hashes is None:
            cron_inventory_hashes = self._inventory_hashes(self._inventory())
        if not isinstance(cron_inventory_hashes, dict) or any(
            not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            for job_id, fingerprint in cron_inventory_hashes.items()
        ):
            raise RuntimeError("Prepared cron inventory receipt is malformed")
        snapshot = self.snapshot()
        payload = {
            "schemaVersion": SCHEMA_VERSION, "runId": str(uuid.uuid4()), "phase": "prepared",
            "contractVersion": INTEGRATION_CONTRACT_VERSION,
            "ownedAssets": [],
            "ownership": self._ownership_payload(),
            "cronMutationStarted": False,
            "runtimeMutationStarted": False,
            "cronInventoryHashesBefore": dict(cron_inventory_hashes),
            "cronUnknownHashesBefore": dict(cron_inventory_hashes),
            **snapshot,
        }
        self.store.write(payload)
        return payload

    def _launchctl(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.launchctl, *args], shell=False, check=check, text=True,
                              capture_output=True, timeout=120)

    def _launchctl_retry(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(len(LAUNCHD_RETRY_DELAYS_SECONDS) + 1):
            result = self._launchctl(args, check=False)
            if result.returncode == 0:
                return result
            if attempt < len(LAUNCHD_RETRY_DELAYS_SECONDS):
                time.sleep(LAUNCHD_RETRY_DELAYS_SECONDS[attempt])
        assert result is not None
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr,
        )

    def _bootstrap_launchd_plist(self, plist_path: Path) -> None:
        domain = f"gui/{os.getuid()}"
        service = f"{domain}/{LAUNCHD_LABEL}"
        self._launchctl_retry(["bootstrap", domain, str(plist_path)])
        self._launchctl_retry(["kickstart", "-k", service])
        self._launchctl_retry(["print", service])

    def activate_launchd(self) -> None:
        domain = f"gui/{os.getuid()}"
        self._launchctl(["bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
        self._bootstrap_launchd_plist(self.paths.launchd_plist)

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

    def synchronize_project_runtime(
        self, transaction: dict[str, Any] | None = None,
    ) -> None:
        if transaction is None:
            raise RuntimeError(
                "Project runtime synchronization requires a durable transaction receipt"
            )
        template = self.skill_source / "assets/knowledge-lancedb-template"
        if template.is_symlink() or not template.is_dir():
            raise RuntimeError("Bundled Qwen project template is missing or unsafe")
        project_assets = {
            Path("src"): "project.src",
            Path("scripts"): "project.scripts",
            Path("package.json"): "project.package_json",
            Path("package-lock.json"): "project.package_lock",
        }
        self._checkpoint_asset_mutation(transaction, project_assets.values())
        specs = {spec.asset_id: spec for spec in self._rollback_asset_specs()}
        for relative, asset_id in project_assets.items():
            source = template / relative
            spec = specs.get(asset_id)
            if spec is None:
                raise RuntimeError("Qwen project runtime asset contract is incomplete")
            self._synchronize_one_project_asset(transaction, spec, source)
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

    def _initial_pre_alert_definition(
        self, staging_description: str, scheduled_at: str,
    ) -> dict[str, Any]:
        return {
            "name": "Qwen local knowledge initial full index",
            "description": staging_description,
            "enabled": False,
            "declarationKey": INITIAL_CRON_DECLARATION_KEY,
            "schedule": {"kind": "at", "at": scheduled_at},
            "payload": {
                "kind": "command",
                "argv": [
                    str(self.paths.project_root / "scripts/knowledge_index_full.sh"),
                    str(self.ownership_manifest),
                ],
                "cwd": str(self.paths.project_root),
                "timeoutSeconds": 86400,
                "noOutputTimeoutSeconds": 1800,
                "outputMaxBytes": 65536,
                "env": {
                    "QWEN_OWNERSHIP_MANIFEST": str(self.ownership_manifest),
                    "QWEN_PYTHON": str(self.python_path),
                    "OPENCLAW_LANCEDB_ROOT": str(self.paths.project_root),
                },
            },
            "delivery": {"mode": "none"},
            "failureAlert": None,
            "sessionTarget": "isolated",
            "sessionKey": None,
            "agentId": None,
            "deleteAfterRun": True,
        }

    def mark_ready_or_schedule_build(
        self, transaction: dict[str, Any],
    ) -> tuple[str, str | None]:
        cli_path = self.paths.project_root / "src/cli.js"
        audit = subprocess.run([str(self.node_path), str(cli_path), "audit", "--mark-ready"],
                               cwd=self.paths.project_root, shell=False, check=False,
                               text=True, capture_output=True, timeout=1800)
        if audit.returncode == 0:
            existing = self._job_by_key(self._inventory(), INITIAL_CRON_DECLARATION_KEY)
            if existing is not None:
                raise RuntimeError("Initial cron appeared without durable add authority")
            return "READY", None
        full_script = self.paths.project_root / "scripts/knowledge_index_full.sh"
        if full_script.is_symlink() or not full_script.is_file():
            raise RuntimeError("Initial index wrapper is missing or unsafe")
        argv = [str(full_script), str(self.ownership_manifest)]
        raw_bucket = transaction.get("cronStagingIntents")
        raw_existing = raw_bucket.get(INITIAL_CRON_DECLARATION_KEY) \
            if isinstance(raw_bucket, dict) else None
        if isinstance(raw_existing, dict) and isinstance(raw_existing.get("scheduledAt"), str):
            scheduled_at = raw_existing["scheduledAt"]
        else:
            scheduled_at = (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        intent = self._ensure_cron_intent(
            transaction,
            bucket_name="cronStagingIntents",
            declaration_key=INITIAL_CRON_DECLARATION_KEY,
            canonical_description=INITIAL_CRON_DESCRIPTION,
            role="initial",
            expected_factory=lambda description: self._initial_pre_alert_definition(
                description, scheduled_at,
            ),
            extra_fields={"scheduledAt": scheduled_at},
        )
        if intent.get("jobId") is not None:
            raise RuntimeError("Initial cron staging id already exists")
        jobs = self._inventory()
        candidate = self._uncheckpointed_intent_candidate(intent, jobs)
        if candidate is not None:
            job_id = self._validate_intent_candidate_authority(
                transaction, intent, candidate,
            )
        else:
            if self._job_by_key(jobs, INITIAL_CRON_DECLARATION_KEY) is not None:
                raise RuntimeError("Initial cron appeared before authorized add")
            payload = self.cli.json([
                "cron", "add", "--name", "Qwen local knowledge initial full index",
                "--description", intent["stagingDescription"], "--session", "isolated",
                "--at", scheduled_at,
                "--command-argv", json.dumps(argv, separators=(",", ":")),
                "--command-cwd", str(self.paths.project_root), "--timeout-seconds", "86400",
                "--no-output-timeout-seconds", "1800", "--output-max-bytes", "65536",
                "--command-env", f"QWEN_OWNERSHIP_MANIFEST={self.ownership_manifest}",
                "--command-env", f"QWEN_PYTHON={self.python_path}",
                "--command-env", f"OPENCLAW_LANCEDB_ROOT={self.paths.project_root}",
                "--declaration-key", INITIAL_CRON_DECLARATION_KEY,
                "--delete-after-run", "--wake", "now", "--no-deliver", "--disabled", "--json",
            ])
            returned_id = self._job_id_from_add(payload)
            after_add = self._inventory()
            candidate = self._uncheckpointed_intent_candidate(intent, after_add)
            if candidate is None:
                raise RuntimeError("Initial cron add lacked an exact staging job")
            job_id = self._validate_intent_candidate_authority(
                transaction, intent, candidate,
                returned_id=returned_id, jobs_before_add=jobs,
                jobs_after_add=after_add,
            )
        self._checkpoint_cron_intent_job_id(
            transaction,
            bucket_name="cronStagingIntents",
            declaration_key=INITIAL_CRON_DECLARATION_KEY,
            job_id=job_id,
            created=True,
        )
        staged = next((job for job in self._inventory() if str(job["id"]) == job_id), None)
        if staged is None or _staging_contract_hash(staged) != intent["preAlertContractSha256"]:
            raise RuntimeError("Initial index job failed staged readback verification")
        alert_spec = self._incremental_spec()
        lifecycle = self._managed_intent_lifecycle_contracts(intent)
        self._edit_cron_with_snapshot_guard(
            job_id,
            alert_spec.alert_args(job_id),
            before=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                lifecycle[0]
            ),
            after=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                lifecycle[1]
            ),
            label="Initial index alert",
        )
        self._edit_cron_with_snapshot_guard(
            job_id,
            [
                "cron", "edit", job_id, "--description", INITIAL_CRON_DESCRIPTION,
                "--disable",
            ],
            before=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                lifecycle[1]
            ),
            after=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                lifecycle[2]
            ),
            label="Initial index description",
        )
        job = self._job_by_key(self._inventory(), INITIAL_CRON_DECLARATION_KEY)
        if job is None or job.get("id") != job_id or not self._initial_job_matches(job, enabled=False):
            raise RuntimeError("Initial index job failed disabled readback verification")
        intent["configured"] = True
        self.store.write(transaction)
        return "INDEX_BUILDING", job_id

    def _initial_job_matches(self, job: dict[str, Any], *, enabled: bool) -> bool:
        try:
            _job_definition(job)
        except RuntimeError:
            return False
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
        alert = job.get("failureAlert") if isinstance(job.get("failureAlert"), dict) else {}
        expected_env = {
            "QWEN_OWNERSHIP_MANIFEST": str(self.ownership_manifest),
            "QWEN_PYTHON": str(self.python_path),
            "OPENCLAW_LANCEDB_ROOT": str(self.paths.project_root),
        }
        return (
            _known_cron_top_level_contract(job)
            and job.get("declarationKey") == INITIAL_CRON_DECLARATION_KEY
            and job.get("name") == "Qwen local knowledge initial full index"
            and job.get("description") == INITIAL_CRON_DESCRIPTION
            and _default_cron_behavior_contract(job)
            and job.get("enabled") is enabled
            and job.get("sessionTarget") == "isolated"
            and job.get("sessionKey") is None
            and job.get("agentId") is None
            and set(schedule) == {"kind", "at"}
            and schedule.get("kind") == "at"
            and isinstance(schedule.get("at"), str) and bool(schedule["at"])
            and set(payload) == {
                "kind", "argv", "cwd", "timeoutSeconds", "noOutputTimeoutSeconds",
                "outputMaxBytes", "env",
            }
            and _job_argv(job) == [
                str(self.paths.project_root / "scripts/knowledge_index_full.sh"),
                str(self.ownership_manifest),
            ]
            and payload.get("kind") == "command"
            and payload.get("cwd") == str(self.paths.project_root)
            and payload.get("timeoutSeconds") == 86400
            and payload.get("noOutputTimeoutSeconds") == 1800
            and payload.get("outputMaxBytes") == 65536
            and _job_env(job) == expected_env
            and "toolsAllow" not in payload
            and _no_delivery_contract(delivery)
            and job.get("deleteAfterRun") is True
            and alert == {
                "after": 1,
                "cooldownMs": 3600000,
                "includeSkipped": False,
                "mode": "announce",
                "channel": self.report_channel,
                "to": self.report_to,
                "accountId": self.report_account_id,
            }
        )

    def _enable_initial_job(self, job_id: str) -> None:
        self._edit_cron_with_snapshot_guard(
            job_id,
            ["cron", "edit", job_id, "--enable"],
            before=lambda job: self._initial_job_matches(job, enabled=False),
            after=lambda job: self._initial_job_matches(job, enabled=True),
            label="Initial index activation",
        )
        job = self._job_by_key(self._inventory(), INITIAL_CRON_DECLARATION_KEY)
        if job is None or job.get("id") != job_id or not self._initial_job_matches(job, enabled=True):
            raise RuntimeError("Initial index job did not enable cleanly")

    def _write_health_receipt(self, *, event: str, status: str) -> None:
        helper = self.paths.project_root / "scripts/backup_health_component.py"
        if helper.is_symlink() or not helper.is_file():
            raise RuntimeError("Qwen health receipt writer is missing or unsafe")
        subprocess.run([
            str(self.python_path), str(helper), "--ownership-manifest", str(self.ownership_manifest),
            "--event", event, "--status", status,
        ], cwd=self.paths.project_root, shell=False, check=True, text=True,
            capture_output=True, timeout=120)

    @staticmethod
    def _restore_pre_alert_definition(
        definition: dict[str, Any], staging_name: str,
    ) -> dict[str, Any]:
        staged = json.loads(json.dumps(_job_definition(definition)))
        staged["name"] = staging_name
        staged["enabled"] = False
        staged["failureAlert"] = None
        return staged

    @classmethod
    def _restore_lifecycle_matches(
        cls,
        job: dict[str, Any],
        definition: dict[str, Any],
        staging_name: str,
    ) -> bool:
        canonical = json.loads(json.dumps(_job_definition(definition)))
        staged = cls._restore_pre_alert_definition(canonical, staging_name)
        staged_alerted = json.loads(json.dumps(staged))
        staged_alerted["failureAlert"] = canonical.get("failureAlert")
        canonical_disabled = json.loads(json.dumps(canonical))
        canonical_disabled["enabled"] = False
        allowed = (staged, staged_alerted, canonical_disabled, canonical)
        actual_hash = _staging_contract_hash(job)
        return any(actual_hash == _staging_contract_hash(candidate) for candidate in allowed)

    def _restore_cron_definition(
        self, definition: dict[str, Any], transaction: dict[str, Any],
    ) -> str:
        if _contains_forbidden_key(definition):
            raise RuntimeError("Refusing to restore an unsafe cron definition")
        name = definition.get("name")
        description = definition.get("description")
        declaration = definition.get("declarationKey")
        schedule = definition.get("schedule") if isinstance(definition.get("schedule"), dict) else {}
        payload = definition.get("payload") if isinstance(definition.get("payload"), dict) else {}
        argv = _job_argv(definition)
        if not isinstance(name, str) or not name or not argv \
                or description is not None \
                and (not isinstance(description, str) or not description) \
                or not isinstance(declaration, str) or not declaration:
            raise RuntimeError("Owned cron rollback definition is incomplete")
        wake_mode = definition.get("wakeMode", "now")
        if wake_mode != "now" or not _default_cron_behavior_contract(definition):
            raise RuntimeError("Owned cron rollback behavior fields are not safely restorable")
        if payload.get("kind") != "command":
            raise RuntimeError("Owned cron rollback definition is not a command")
        if "toolsAllow" in payload:
            raise RuntimeError("Owned command cron rollback tools policy is not safely restorable")
        intent = self._ensure_restore_cron_intent(transaction, definition)
        args = ["cron", "add", "--name", intent["stagingName"]]
        if isinstance(description, str):
            args.extend(["--description", description])
        session_target = definition.get("sessionTarget")
        if isinstance(session_target, str) and session_target:
            args.extend(["--session", session_target])
        agent_id = definition.get("agentId")
        if isinstance(agent_id, str) and agent_id:
            args.extend(["--agent", agent_id])
        session_key = definition.get("sessionKey")
        if isinstance(session_key, str) and session_key:
            args.extend(["--session-key", session_key])
        kind = schedule.get("kind")
        if kind == "cron" and isinstance(schedule.get("expr"), str):
            args.extend(["--cron", schedule["expr"]])
            if isinstance(schedule.get("tz"), str) and schedule["tz"]:
                args.extend(["--tz", schedule["tz"]])
            if schedule.get("staggerMs", 0) == 0:
                args.append("--exact")
        elif kind == "at" and isinstance(schedule.get("at"), str):
            args.extend(["--at", schedule["at"]])
        elif kind == "every" and isinstance(schedule.get("everyMs"), int):
            args.extend(["--every", f"{schedule['everyMs']}ms"])
        else:
            raise RuntimeError("Owned cron rollback schedule is unsupported")
        args.extend(["--command-argv", json.dumps(argv, separators=(",", ":"))])
        if isinstance(payload.get("cwd"), str):
            args.extend(["--command-cwd", payload["cwd"]])
        for option, key in (
            ("--timeout-seconds", "timeoutSeconds"),
            ("--no-output-timeout-seconds", "noOutputTimeoutSeconds"),
            ("--output-max-bytes", "outputMaxBytes"),
        ):
            if type(payload.get(key)) is int:
                args.extend([option, str(payload[key])])
        for key, value in sorted(_job_env(definition).items()):
            args.extend(["--command-env", f"{key}={value}"])
        args.extend(["--declaration-key", declaration, "--wake", wake_mode])
        delivery = definition.get("delivery") if isinstance(definition.get("delivery"), dict) else {}
        if delivery.get("mode") == "none":
            args.append("--no-deliver")
        elif delivery.get("mode") == "announce":
            args.append("--announce")
            if isinstance(delivery.get("channel"), str):
                args.extend(["--channel", delivery["channel"]])
            if isinstance(delivery.get("to"), str):
                args.extend(["--to", delivery["to"]])
            if isinstance(delivery.get("accountId"), str):
                args.extend(["--account", delivery["accountId"]])
        if definition.get("deleteAfterRun") is True:
            args.append("--delete-after-run")
        args.extend(["--disabled", "--json"])
        jobs = self._inventory()
        checkpointed_id = intent.get("jobId")
        if isinstance(checkpointed_id, str):
            current = next(
                (job for job in jobs if str(job["id"]) == checkpointed_id), None,
            )
            if current is None or not self._restore_lifecycle_matches(
                current, definition, intent["stagingName"],
            ):
                raise RuntimeError("Checkpointed rollback restore job drifted")
            job_id = checkpointed_id
        else:
            candidate = self._uncheckpointed_restore_intent_candidate(intent, jobs)
            if candidate is not None:
                job_id = self._validate_intent_candidate_authority(
                    transaction, intent, candidate,
                )
            else:
                if self._job_by_key(jobs, declaration) is not None:
                    raise RuntimeError("Rollback restore declaration appeared without authority")
                returned_id = self._job_id_from_add(self.cli.json(args))
                after_add = self._inventory()
                candidate = self._uncheckpointed_restore_intent_candidate(
                    intent, after_add,
                )
                if candidate is None:
                    raise RuntimeError("Rollback restore add lacked an exact staging job")
                job_id = self._validate_intent_candidate_authority(
                    transaction, intent, candidate,
                    returned_id=returned_id, jobs_before_add=jobs,
                    jobs_after_add=after_add,
                )
            self._checkpoint_cron_intent_job_id(
                transaction,
                bucket_name="cronRestoreIntents",
                declaration_key=declaration,
                job_id=job_id,
                created=False,
            )
        current = next((job for job in self._inventory() if str(job["id"]) == job_id), None)
        if current is None or not self._restore_lifecycle_matches(
            current, definition, intent["stagingName"],
        ):
            raise RuntimeError("Rollback restore job failed staged lifecycle verification")
        disabled_definition = json.loads(json.dumps(_job_definition(definition)))
        disabled_definition["enabled"] = False
        if _staging_contract_hash(current) == _staging_contract_hash(disabled_definition):
            intent["configured"] = True
            self.store.write(transaction)
            return job_id
        if _staging_contract_hash(current) == _staging_contract_hash(definition):
            self._edit_cron_with_snapshot_guard(
                job_id,
                ["cron", "edit", job_id, "--disable"],
                before=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                    definition
                ),
                after=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                    disabled_definition
                ),
                label="Rollback restore safe disable",
            )
            disabled = next(
                (job for job in self._inventory() if str(job["id"]) == job_id), None,
            )
            if disabled is None or _job_contract_hash(disabled) != _job_contract_hash(
                disabled_definition
            ):
                raise RuntimeError("Rollback restore job failed safe disable verification")
            intent["configured"] = True
            self.store.write(transaction)
            return job_id
        alert = definition.get("failureAlert") if isinstance(definition.get("failureAlert"), dict) else None
        staged_before_alert = self._restore_pre_alert_definition(
            definition, intent["stagingName"],
        )
        staged_before_name = staged_before_alert
        if alert:
            edit = [
                "cron", "edit", job_id, "--failure-alert",
                "--failure-alert-after", str(alert.get("after", 1)),
                "--failure-alert-cooldown", f"{int(alert.get('cooldownMs', 3600000))}ms",
            ]
            if isinstance(alert.get("mode"), str):
                edit.extend(["--failure-alert-mode", alert["mode"]])
            edit.append("--failure-alert-include-skipped" if alert.get("includeSkipped") is True
                        else "--failure-alert-exclude-skipped")
            if isinstance(alert.get("channel"), str):
                edit.extend(["--failure-alert-channel", alert["channel"]])
            if isinstance(alert.get("to"), str):
                edit.extend(["--failure-alert-to", alert["to"]])
            if isinstance(alert.get("accountId"), str):
                edit.extend(["--failure-alert-account-id", alert["accountId"]])
            edit.append("--disable")
            staged_after_alert = json.loads(json.dumps(staged_before_alert))
            staged_after_alert["failureAlert"] = alert
            self._edit_cron_with_snapshot_guard(
                job_id,
                edit,
                before=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                    staged_before_alert
                ),
                after=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                    staged_after_alert
                ),
                label="Rollback restore alert",
            )
            staged_before_name = staged_after_alert
        self._edit_cron_with_snapshot_guard(
            job_id,
            ["cron", "edit", job_id, "--name", name, "--disable"],
            before=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                staged_before_name
            ),
            after=lambda job: _staging_contract_hash(job) == _staging_contract_hash(
                disabled_definition
            ),
            label="Rollback restore canonical name",
        )
        restored = next((job for job in self._inventory() if str(job["id"]) == job_id), None)
        if restored is None or _job_contract_hash(restored) != _job_contract_hash(
            disabled_definition
        ):
            raise RuntimeError("Rollback restore job failed disabled readback verification")
        intent["configured"] = True
        self.store.write(transaction)
        return job_id

    def _verify_rollback_cron_state(
        self,
        *,
        prior_definitions: list[dict[str, Any]],
        restored_ids: list[str],
        unknown_hashes_before: dict[str, str],
        force_disabled: bool = False,
    ) -> None:
        if len(restored_ids) != len(prior_definitions) or len(set(restored_ids)) != len(restored_ids):
            raise RuntimeError("Rollback cron restoration set is incomplete")
        jobs = self._inventory()
        by_id = {str(job["id"]): job for job in jobs}
        for definition, restored_id in zip(prior_definitions, restored_ids):
            restored = by_id.get(restored_id)
            expected = json.loads(json.dumps(_job_definition(definition)))
            if force_disabled:
                expected["enabled"] = False
            if restored is None or _job_contract_hash(restored) != _job_contract_hash(expected):
                raise RuntimeError("Rollback did not restore an owned cron definition exactly")
        unknown_after = {
            job_id: _job_contract_hash(by_id[job_id], include_id=True)
            for job_id in unknown_hashes_before if job_id in by_id
        }
        if unknown_after != unknown_hashes_before:
            raise RuntimeError("Unknown cron definitions changed during rollback")
        expected_ids = set(unknown_hashes_before) | set(restored_ids)
        if set(by_id) != expected_ids or len(jobs) != len(expected_ids):
            raise RuntimeError("Rollback cron inventory contains missing or unexpected jobs")

    def _activate_restored_cron_definitions(
        self,
        *,
        prior_definitions: list[dict[str, Any]],
        restored_ids: list[str],
        unknown_hashes_before: dict[str, str],
    ) -> None:
        if len(prior_definitions) != len(restored_ids):
            raise RuntimeError("Rollback cron restoration set is incomplete")
        enabled = [
            (definition, restored_id)
            for definition, restored_id in zip(prior_definitions, restored_ids)
            if definition.get("enabled", True) is True
        ]
        try:
            for definition, job_id in enabled:
                disabled_definition = json.loads(json.dumps(_job_definition(definition)))
                disabled_definition["enabled"] = False
                self._edit_cron_with_snapshot_guard(
                    job_id,
                    ["cron", "edit", job_id, "--enable"],
                    before=lambda job, expected=disabled_definition: _job_contract_hash(
                        job
                    ) == _job_contract_hash(expected),
                    after=lambda job, expected=definition: _job_contract_hash(
                        job
                    ) == _job_contract_hash(expected),
                    label="Rollback restore activation",
                )
            self._verify_rollback_cron_state(
                prior_definitions=prior_definitions,
                restored_ids=restored_ids,
                unknown_hashes_before=unknown_hashes_before,
            )
        except Exception as activation_error:
            compensation_errors: list[Exception] = []
            for definition, job_id in zip(prior_definitions, restored_ids):
                try:
                    canonical = _job_definition(definition)
                    disabled = json.loads(json.dumps(canonical))
                    disabled["enabled"] = False
                    self._edit_cron_with_snapshot_guard(
                        job_id,
                        ["cron", "edit", job_id, "--disable"],
                        before=lambda job, expected=canonical, safe=disabled: (
                            _job_contract_hash(job) in {
                                _job_contract_hash(expected), _job_contract_hash(safe),
                            }
                        ),
                        after=lambda job, expected=disabled: _job_contract_hash(
                            job
                        ) == _job_contract_hash(expected),
                        label="Rollback activation compensation",
                    )
                except Exception as error:
                    compensation_errors.append(error)
            try:
                self._verify_rollback_cron_state(
                    prior_definitions=prior_definitions,
                    restored_ids=restored_ids,
                    unknown_hashes_before=unknown_hashes_before,
                    force_disabled=True,
                )
            except Exception as error:
                compensation_errors.append(error)
            if compensation_errors:
                raise RuntimeError(
                    "Rollback cron activation compensation was incomplete"
                ) from activation_error
            raise

    def _verify_plugin_skill_gateway(self) -> tuple[bool, bool, bool]:
        plugin = self.cli.json(["plugins", "inspect", PLUGIN_ID, "--runtime", "--json"])
        skill = self.cli.json(["skills", "info", SKILL_ID, "--agent", self.agent, "--json"])
        plugin_text = json.dumps(plugin, sort_keys=True)
        skill_text = json.dumps(skill, sort_keys=True)
        if TOOL_NAME not in plugin_text or PLUGIN_ID not in plugin_text:
            raise RuntimeError("local_knowledge_search tool owner is not loaded")
        if SKILL_ID not in skill_text or not (skill.get("eligible") is True or '"eligible": true' in skill_text.lower()):
            raise RuntimeError("Local knowledge skill is not eligible")
        gateway = self.cli.json(["gateway", "status", "--require-rpc", "--json"])
        return True, True, bool(gateway)

    def _verify_legacy_contract(self, manifest: dict[str, Any]) -> dict[str, Any]:
        jobs = _cron_jobs(self.cli.json(["cron", "list", "--all", "--json"]))
        enabled_jobs = [job for job in jobs if job.get("enabled", True) is not False]
        matching = [
            job for job in enabled_jobs if job.get("declarationKey") == CRON_DECLARATION_KEY
        ]
        incremental_script = self.paths.project_root / "scripts/knowledge_index_incremental.sh"
        command_matches = [
            job for job in enabled_jobs if _job_targets_exact_script(job, incremental_script)
        ]
        if len(matching) != 1:
            raise RuntimeError("Incremental cron declaration is missing or duplicated")
        if len(command_matches) != 1 or command_matches[0] is not matching[0]:
            raise RuntimeError("Managed incremental cron command is missing or duplicated")
        plugin_ok, skill_ok, gateway_ok = self._verify_plugin_skill_gateway()
        return {
            "ok": True, "phase": manifest.get("phase"), "pluginLoaded": plugin_ok, "skillEligible": skill_ok,
            "incrementalCronUnique": True, "gateway": gateway_ok, "indexState": manifest.get("indexState"),
            "contractVersion": manifest.get("contractVersion", 1), "upgradeRequired": True,
        }

    def _health_receipt_status(self) -> str:
        path = self.health_receipt_path
        descriptor: int | None = None
        try:
            with self._open_private_directory(path.parent) as parent_fd:
                self._validate_restricted_directory(os.fstat(parent_fd))
                descriptor = os.open(
                    path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                with os.fdopen(descriptor, "rb", closefd=True) as handle:
                    descriptor = None
                    metadata = os.fstat(handle.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() \
                            or metadata.st_nlink != 1 or metadata.st_mode & 0o077 \
                            or metadata.st_size > HEALTH_RECEIPT_MAX_BYTES:
                        return "warning"
                    encoded = handle.read(HEALTH_RECEIPT_MAX_BYTES + 1)
                    after = os.fstat(handle.fileno())
                    self._assert_stable_file(metadata, after)
            if len(encoded) > HEALTH_RECEIPT_MAX_BYTES:
                return "warning"
            payload = json.loads(encoded.decode("utf-8"))
            if not isinstance(payload, dict) or _contains_forbidden_key(payload):
                return "warning"
            if set(payload) != {
                "schema", "component", "producer", "declarationKey", "status", "checkedAt",
                "freshness", "summary", "checks", "metrics", "anomalies", "pending",
            }:
                return "warning"
            checked = datetime.fromisoformat(str(payload.get("checkedAt", "")).replace("Z", "+00:00"))
            if checked.tzinfo is None:
                return "warning"
            freshness = payload.get("freshness")
            if not isinstance(freshness, dict):
                return "warning"
            max_age = freshness.get("maxAgeSeconds")
            declaration = payload.get("declarationKey")
            age = (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
            if payload.get("schema") != HEALTH_RECEIPT_SCHEMA or payload.get("component") != "qwen-local" \
                    or payload.get("producer") != "qwen-local" \
                    or declaration not in {CRON_DECLARATION_KEY, SNAPSHOT_CRON_DECLARATION_KEY,
                                           INITIAL_CRON_DECLARATION_KEY} \
                    or freshness != {
                        "status": "current", "maxAgeSeconds": HEALTH_RECEIPT_MAX_AGE_SECONDS,
                    } \
                    or age < -300 or age > HEALTH_RECEIPT_MAX_AGE_SECONDS \
                    or payload.get("status") not in {"ok", "warning", "error", "pending"} \
                    or not isinstance(payload.get("summary"), str) \
                    or not isinstance(payload.get("metrics"), dict) \
                    or set(payload["metrics"]) - {"rows"}:
                return "warning"
            if "rows" in payload["metrics"] and (
                type(payload["metrics"]["rows"]) is not int or payload["metrics"]["rows"] < 0
            ):
                return "warning"
            for key in ("checks", "anomalies", "pending"):
                if not isinstance(payload.get(key), list) or len(payload[key]) > HEALTH_RECEIPT_MAX_ITEMS:
                    return "warning"
            for check in payload["checks"]:
                if not isinstance(check, dict) or set(check) != {"key", "status", "summary"} \
                        or check.get("status") not in {"ok", "warning", "error", "pending"} \
                        or not all(isinstance(check.get(key), str) for key in ("key", "summary")):
                    return "warning"
            anomaly_fields = {"code", "summary", "impact", "dataLoss", "repairStatus"}
            for anomaly in payload["anomalies"]:
                if not isinstance(anomaly, dict) or set(anomaly) != anomaly_fields \
                        or anomaly.get("dataLoss") not in {"no", "yes", "unknown"} \
                        or not all(isinstance(anomaly.get(key), str) for key in anomaly_fields):
                    return "warning"
            if any(not isinstance(item, str) for item in payload["pending"]):
                return "warning"
            return "ok" if payload.get("status") == "ok" else str(payload.get("status", "warning"))
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError,
                UnicodeError, json.JSONDecodeError):
            return "warning"
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _verify_runtime_contract_files(self) -> None:
        template = self.skill_source / "assets/knowledge-lancedb-template"
        for relative in (
            Path("scripts/backup_health_component.py"),
            Path("scripts/index_lock.py"),
            Path("scripts/run_verified_snapshot.py"),
            Path("scripts/snapshot_knowledge_assets.py"),
            Path("scripts/knowledge_index_incremental.sh"),
            Path("scripts/knowledge_index_full.sh"),
        ):
            source = template / relative
            target = self.paths.project_root / relative
            if source.is_symlink() or target.is_symlink() or not source.is_file() or not target.is_file() \
                    or sha256_file(source) != sha256_file(target):
                raise RuntimeError("Installed Qwen runtime contract files do not match the reviewed package")

    def _verify_local_source_map(self) -> None:
        source_map = json.loads((self.paths.project_root / "config/source-map.json").read_text(encoding="utf-8"))
        embedding = source_map.get("embedding") if isinstance(source_map, dict) else None
        endpoint = urlparse(str(embedding.get("endpoint", ""))) if isinstance(embedding, dict) else None
        try:
            endpoint_port = endpoint.port if endpoint is not None else None
        except ValueError as error:
            raise RuntimeError("Qwen runtime loopback endpoint is malformed") from error
        if not isinstance(embedding, dict) or embedding.get("provider") != "qwen-local" \
                or endpoint is None or endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" \
                or endpoint.username is not None or endpoint.password is not None \
                or endpoint_port is None or endpoint.path not in ("", "/") \
                or endpoint.params or endpoint.query or endpoint.fragment \
                or any("fallback" in str(key).lower() and value not in (None, False, "", [], {})
                       for key, value in embedding.items()):
            raise RuntimeError("Qwen runtime is not loopback-only or contains a fallback")

    def _verify_snapshot_wrapper_contract(self) -> None:
        wrapper = self.paths.project_root / "scripts/run_verified_snapshot.py"
        subprocess.run([
            str(self.python_path), str(wrapper), "--ownership-manifest", str(self.ownership_manifest),
            "--check-contract",
        ], cwd=self.paths.project_root, shell=False, check=True, text=True,
            capture_output=True, timeout=120)

    def _verify_success_cron_inventory(
        self,
        transaction: dict[str, Any],
        *,
        recurring_enabled: bool,
        initial_enabled: bool,
        jobs: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        self._validate_replacement_receipt_graph(transaction)
        current = self._inventory() if jobs is None else jobs
        by_id = {str(job["id"]): job for job in current}
        unknown_hashes = transaction.get("cronUnknownHashesBefore")
        gemini_hashes = transaction.get("cronPreservedGeminiHashesAfterQuiesce")
        if not isinstance(unknown_hashes, dict) or not isinstance(gemini_hashes, dict) \
                or any(
                    not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    for receipt in (unknown_hashes, gemini_hashes)
                    for job_id, fingerprint in receipt.items()
                ) \
                or set(unknown_hashes) & set(gemini_hashes):
            raise RuntimeError("Cron success inventory receipt is malformed")
        for receipt, message in (
            (unknown_hashes, "Unknown cron definitions changed during integration"),
            (gemini_hashes, "Quiesced Gemini cron definitions changed during integration"),
        ):
            actual = {
                job_id: _job_contract_hash(by_id[job_id], include_id=True)
                for job_id in receipt if job_id in by_id
            }
            if actual != receipt:
                raise RuntimeError(message)

        recurring = (
            (transaction.get("cronId"), self._incremental_spec()),
            (transaction.get("snapshotCronId"), self._snapshot_spec()),
        )
        recurring_ids: list[str] = []
        for raw_id, spec in recurring:
            if not isinstance(raw_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(raw_id):
                raise RuntimeError("Recurring cron success id receipt is malformed")
            job = by_id.get(raw_id)
            if job is None or not _job_matches_spec(
                job, spec, require_enabled=recurring_enabled,
            ):
                raise RuntimeError("Recurring cron success contract drifted")
            recurring_ids.append(raw_id)
        if len(set(recurring_ids)) != 2:
            raise RuntimeError("Recurring cron success ids are not unique")

        expected_managed_ids = set(recurring_ids)
        index_state = transaction.get("indexState")
        initial_id = transaction.get("initialIndexJobId")
        if index_state == "INDEX_BUILDING":
            if not isinstance(initial_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(initial_id):
                raise RuntimeError("Initial cron success id receipt is malformed")
            initial = by_id.get(initial_id)
            if initial is None or not self._initial_job_matches(
                initial, enabled=initial_enabled,
            ):
                raise RuntimeError("Initial cron success contract drifted")
            expected_managed_ids.add(initial_id)
        elif index_state == "READY":
            if initial_id is not None or any(
                job.get("declarationKey") == INITIAL_CRON_DECLARATION_KEY
                for job in current
            ):
                raise RuntimeError("Unexpected initial cron exists for a ready index")
        else:
            raise RuntimeError("Cron success index state is invalid")

        managed_ids_after = transaction.get("managedCronIdsAfter")
        if not isinstance(managed_ids_after, list) \
                or any(
                    not isinstance(value, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(value)
                    for value in managed_ids_after
                ) \
                or set(managed_ids_after) != expected_managed_ids \
                or len(managed_ids_after) != len(expected_managed_ids):
            raise RuntimeError("Managed cron success id receipt is incomplete")
        intents = self._validated_cron_intent_receipts(
            transaction, "cronStagingIntents",
        )
        expected_intent_keys = {CRON_DECLARATION_KEY, SNAPSHOT_CRON_DECLARATION_KEY}
        if index_state == "INDEX_BUILDING":
            expected_intent_keys.add(INITIAL_CRON_DECLARATION_KEY)
        if not isinstance(intents, dict) or set(intents) != expected_intent_keys:
            raise RuntimeError("Cron staging intent topology is incomplete")
        expected_by_key = {
            CRON_DECLARATION_KEY: transaction["cronId"],
            SNAPSHOT_CRON_DECLARATION_KEY: transaction["snapshotCronId"],
        }
        if index_state == "INDEX_BUILDING":
            expected_by_key[INITIAL_CRON_DECLARATION_KEY] = initial_id
        if any(
            not isinstance(intents.get(key), dict)
            or intents[key].get("jobId") != expected_id
            or intents[key].get("configured") is not True
            for key, expected_id in expected_by_key.items()
        ):
            raise RuntimeError("Cron staging intent id receipt is incomplete")
        for declaration_key, expected_id in expected_by_key.items():
            enabled = (
                initial_enabled
                if declaration_key == INITIAL_CRON_DECLARATION_KEY
                else recurring_enabled
            )
            lifecycle = self._managed_intent_lifecycle_contracts(
                intents[declaration_key]
            )
            expected = json.loads(json.dumps(lifecycle[3 if enabled else 2]))
            expected["id"] = expected_id
            if _job_contract_hash(by_id[expected_id], include_id=True) != _job_contract_hash(
                expected, include_id=True,
            ):
                raise RuntimeError("Cron success intent contract drifted")

        expected_ids = set(unknown_hashes) | set(gemini_hashes) | expected_managed_ids
        if set(by_id) != expected_ids or len(current) != len(expected_ids):
            raise RuntimeError("Cron success inventory contains missing or unexpected jobs")
        return self._inventory_hashes(current)

    @staticmethod
    def _validate_commit_cron_receipt_metadata(
        transaction: dict[str, Any],
    ) -> dict[str, str]:
        if transaction.get("cronContractHashVersion") != CRON_CONTRACT_HASH_VERSION:
            raise RuntimeError("Committed cron hash contract version is unsupported")
        receipt = transaction.get("cronCommitInventoryHashes")
        if not isinstance(receipt, dict) or any(
            not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            for job_id, fingerprint in receipt.items()
        ):
            raise RuntimeError("Committed cron inventory receipt is malformed")
        if transaction.get("cronCommitUnknownHashes") != transaction.get("cronUnknownHashesBefore") \
                or transaction.get("cronCommitGeminiHashes") != transaction.get(
                    "cronPreservedGeminiHashesAfterQuiesce"
                ) \
                or transaction.get("cronCommitTopologyVerified") is not True:
            raise RuntimeError("Committed cron topology receipt is incomplete")
        managed_ids = transaction.get("managedCronIdsAfter")
        unknown = transaction.get("cronCommitUnknownHashes")
        gemini = transaction.get("cronCommitGeminiHashes")
        if not isinstance(managed_ids, list) \
                or any(
                    not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    for job_id in managed_ids
                ) \
                or len(managed_ids) != len(set(managed_ids)) \
                or not isinstance(unknown, dict) or not isinstance(gemini, dict) \
                or any(
                    not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    for item in (unknown, gemini)
                    for job_id, fingerprint in item.items()
                ) \
                or set(managed_ids) & set(unknown) \
                or set(managed_ids) & set(gemini) \
                or set(unknown) & set(gemini) \
                or set(receipt) != set(managed_ids) | set(unknown) | set(gemini) \
                or any(
                    receipt.get(job_id) != fingerprint
                    for subset in (unknown, gemini)
                    for job_id, fingerprint in subset.items()
                ):
            raise RuntimeError("Committed cron topology id receipt is inconsistent")
        return receipt

    def _verify_commit_cron_receipt(self, transaction: dict[str, Any]) -> None:
        if transaction.get("phase") != "commit_closeout_pending":
            raise RuntimeError("Cron commit closeout phase is invalid")
        receipt = self._validate_commit_cron_receipt_metadata(transaction)
        jobs = self._inventory()
        actual = self._verify_success_cron_inventory(
            transaction,
            recurring_enabled=True,
            initial_enabled=True,
            jobs=jobs,
        )
        if actual != receipt:
            raise RuntimeError("Cron inventory drifted during commit closeout")

    def _verify_committed_gemini_receipt(
        self, transaction: dict[str, Any], *, jobs: list[dict[str, Any]] | None = None,
    ) -> None:
        """Keep the durable Gemini quiescence promise across later repair attempts."""
        self._validate_commit_cron_receipt_metadata(transaction)
        current_jobs = self._inventory() if jobs is None else jobs
        current_gemini = {
            str(job["id"]): _job_contract_hash(job, include_id=True)
            for job in self._owned_gemini_jobs_exact(current_jobs)
        }
        if current_gemini != transaction.get("cronCommitGeminiHashes"):
            raise RuntimeError("Committed quiesced Gemini cron inventory receipt drifted")

    def _verify_legacy_committed_v3_repair_authority(
        self, transaction: dict[str, Any], jobs: list[dict[str, Any]],
    ) -> None:
        """Authorize a one-time upgrade of the pre-commit-receipt v3 contract."""
        new_receipt_fields = {
            "cronContractHashVersion", "cronCommitInventoryHashes",
            "cronCommitUnknownHashes", "cronCommitGeminiHashes",
            "cronPreservedGeminiHashesAfterQuiesce", "cronCommitTopologyVerified",
        }
        if any(field in transaction for field in new_receipt_fields) \
                or transaction.get("phase") != "committed" \
                or transaction.get("contractVersion") != INTEGRATION_CONTRACT_VERSION \
                or transaction.get("ownership") != self._ownership_payload():
            raise RuntimeError("Legacy committed v3 repair authority is not applicable")
        raw_definitions = transaction.get("cronDefinitionsBefore")
        target_ids = transaction.get("cronTargetIdsBefore")
        inventory_before = transaction.get("cronInventoryHashesBefore")
        unknown_before = transaction.get("cronUnknownHashesBefore")
        disabled_gemini = transaction.get("disabledGeminiJobs")
        if not isinstance(raw_definitions, list) \
                or any(not isinstance(item, dict) for item in raw_definitions) \
                or not isinstance(target_ids, list) \
                or not isinstance(inventory_before, dict) \
                or not isinstance(unknown_before, dict) \
                or not isinstance(disabled_gemini, list) \
                or any(
                    not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    for receipt in (inventory_before, unknown_before)
                    for job_id, fingerprint in receipt.items()
                ):
            raise RuntimeError("Legacy committed v3 receipt graph is malformed")
        definitions = [_job_definition(item) for item in raw_definitions]
        definitions_by_id = {str(item["id"]): item for item in definitions}
        raw_by_id = {str(item.get("id")): item for item in raw_definitions}
        target_set = set(target_ids)
        if len(definitions_by_id) != len(definitions) \
                or any(
                    not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    for job_id in target_ids
                ) \
                or len(target_set) != len(target_ids) \
                or target_set != set(definitions_by_id) \
                or set(inventory_before) != target_set | set(unknown_before) \
                or target_set & set(unknown_before):
            raise RuntimeError("Legacy committed v3 receipt graph is inconsistent")
        for job_id in definitions_by_id:
            if inventory_before.get(job_id) != _legacy_v3_job_contract_hash(
                raw_by_id[job_id], include_id=True,
            ):
                raise RuntimeError("Legacy committed v3 definition receipt drifted")

        prior_gemini = [
            definition for definition in definitions
            if definition.get("declarationKey") == GEMINI_DECLARATION_KEY
        ]
        expected_disabled_receipt = sorted(
            ({"id": str(job["id"]), "wasEnabled": True} for job in prior_gemini
             if job.get("enabled", True) is True),
            key=lambda item: item["id"],
        )
        if sorted(disabled_gemini, key=lambda item: str(item) if not isinstance(item, dict)
                  else str(item.get("id"))) != expected_disabled_receipt:
            raise RuntimeError("Legacy committed v3 Gemini disable receipt drifted")
        current_gemini = self._owned_gemini_jobs_exact(jobs)
        expected_gemini: dict[str, str] = {}
        for definition in prior_gemini:
            disabled = json.loads(json.dumps(definition))
            disabled["enabled"] = False
            expected_gemini[str(definition["id"])] = _job_contract_hash(
                disabled, include_id=True,
            )
        actual_gemini = {
            str(job["id"]): _job_contract_hash(job, include_id=True)
            for job in current_gemini
        }
        if actual_gemini != expected_gemini:
            raise RuntimeError("Legacy committed v3 Gemini contract drifted")

        incremental = self._job_by_key(jobs, CRON_DECLARATION_KEY)
        snapshot = self._job_by_key(jobs, SNAPSHOT_CRON_DECLARATION_KEY)
        cron_id = transaction.get("cronId")
        snapshot_id = transaction.get("snapshotCronId")
        if incremental is None or snapshot is None \
                or str(incremental.get("id")) != cron_id \
                or str(snapshot.get("id")) != snapshot_id \
                or not _job_matches_spec(
                    incremental, self._incremental_spec(), require_enabled=True,
                ) \
                or not _job_matches_spec(
                    snapshot, self._snapshot_spec(), require_enabled=True,
                ):
            raise RuntimeError("Legacy committed v3 managed cron receipt drifted")
        initial = self._job_by_key(jobs, INITIAL_CRON_DECLARATION_KEY)
        if transaction.get("indexState") == "READY":
            if initial is not None or transaction.get("initialIndexJobId") is not None:
                raise RuntimeError("Legacy committed v3 READY topology drifted")
        elif transaction.get("indexState") == "INDEX_BUILDING":
            initial_id = transaction.get("initialIndexJobId")
            if not isinstance(initial_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(initial_id) \
                    or initial is None or str(initial.get("id")) != initial_id \
                    or not self._initial_job_matches(initial, enabled=True):
                raise RuntimeError("Legacy committed v3 initial cron receipt drifted")
        else:
            raise RuntimeError("Legacy committed v3 index state is invalid")

    def _verify_activation_pending(self, transaction: dict[str, Any]) -> None:
        if transaction.get("phase") != "activation_pending" or transaction.get("ownership") != self._ownership_payload():
            raise RuntimeError("Qwen activation transaction is not ready for final verification")
        jobs, legacy = self._preflight_cron_inventory()
        if legacy:
            raise RuntimeError("Legacy snapshot declaration remains during activation")
        self._verify_approved_collision_receipt(transaction, jobs)
        self._verify_success_cron_inventory(
            transaction,
            recurring_enabled=True,
            initial_enabled=True,
            jobs=jobs,
        )
        self._verify_local_source_map()
        self._verify_runtime_contract_files()
        self._verify_snapshot_wrapper_contract()
        self._verify_plugin_skill_gateway()
        if self._health_receipt_status() not in {"ok", "pending"}:
            raise RuntimeError("Qwen health receipt failed activation verification")

    def verify(self) -> dict[str, Any]:
        manifest = self.store.read()
        if manifest.get("contractVersion") != INTEGRATION_CONTRACT_VERSION:
            return self._verify_legacy_contract(manifest)
        if manifest.get("phase") != "committed" or manifest.get("ownership") != self._ownership_payload():
            raise RuntimeError("Qwen integration ownership contract is incomplete or drifted")
        committed_inventory = self._validate_commit_cron_receipt_metadata(manifest)
        jobs, legacy = self._preflight_cron_inventory()
        if legacy:
            raise RuntimeError("Legacy snapshot declaration remains after committed reconciliation")
        self._verify_approved_collision_receipt(manifest, jobs)
        incremental = self._job_by_key(jobs, CRON_DECLARATION_KEY)
        snapshot = self._job_by_key(jobs, SNAPSHOT_CRON_DECLARATION_KEY)
        if incremental is None or not _job_matches_spec(incremental, self._incremental_spec(), require_enabled=True):
            raise RuntimeError("Incremental cron does not match the exact managed contract")
        if snapshot is None or not _job_matches_spec(snapshot, self._snapshot_spec(), require_enabled=True):
            raise RuntimeError("Snapshot cron does not match the exact managed contract")
        cron_id = manifest.get("cronId")
        snapshot_cron_id = manifest.get("snapshotCronId")
        if not isinstance(cron_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(cron_id) \
                or str(incremental.get("id")) != cron_id:
            raise RuntimeError("Incremental cron id does not match the committed receipt")
        if not isinstance(snapshot_cron_id, str) \
                or not SAFE_CRON_JOB_ID_RE.fullmatch(snapshot_cron_id) \
                or str(snapshot.get("id")) != snapshot_cron_id:
            raise RuntimeError("Snapshot cron id does not match the committed receipt")
        initial = self._job_by_key(jobs, INITIAL_CRON_DECLARATION_KEY)
        index_state = manifest.get("indexState")
        expected_managed_ids = {cron_id, snapshot_cron_id}
        if index_state == "INDEX_BUILDING":
            initial_id = manifest.get("initialIndexJobId")
            if not isinstance(initial_id, str) \
                    or not SAFE_CRON_JOB_ID_RE.fullmatch(initial_id) \
                    or initial is None or str(initial.get("id")) != initial_id \
                    or not self._initial_job_matches(initial, enabled=True):
                raise RuntimeError(
                    "Pending initial index job does not match the exact managed contract"
                )
            expected_managed_ids.add(initial_id)
        elif index_state == "READY":
            if initial is not None or manifest.get("initialIndexJobId") is not None:
                raise RuntimeError("Unexpected initial cron exists for a ready index")
        else:
            raise RuntimeError("Committed Qwen index state is invalid")
        managed_ids = manifest.get("managedCronIdsAfter")
        if not isinstance(managed_ids, list) or len(managed_ids) != len(expected_managed_ids) \
                or set(managed_ids) != expected_managed_ids:
            raise RuntimeError("Managed cron ids do not match the committed topology")
        current_inventory = self._inventory_hashes(jobs)
        if any(
            committed_inventory.get(job_id) != current_inventory.get(job_id)
            for job_id in expected_managed_ids
        ):
            raise RuntimeError("Committed managed cron inventory receipt drifted")
        self._verify_committed_gemini_receipt(manifest, jobs=jobs)
        self._verify_local_source_map()
        self._verify_runtime_contract_files()
        self._verify_snapshot_wrapper_contract()
        plugin_ok, skill_ok, gateway_ok = self._verify_plugin_skill_gateway()
        return {
            "ok": True,
            "phase": manifest.get("phase"),
            "contractVersion": INTEGRATION_CONTRACT_VERSION,
            "pluginLoaded": plugin_ok,
            "skillEligible": skill_ok,
            "gateway": gateway_ok,
            "incrementalCronUnique": True,
            "snapshotCronUnique": True,
            "indexState": index_state,
            "healthReceiptStatus": self._health_receipt_status(),
        }

    def integrate(self, runtime_manifest: dict[str, Any]) -> dict[str, Any]:
        with self._integration_lock():
            return self._integrate_locked(runtime_manifest)

    def _disarm_activation_fail_safe_after_verify(
        self, transaction: dict[str, Any],
    ) -> None:
        persisted = self.store.read()
        if persisted != transaction \
                or persisted.get("phase") != "committed" \
                or persisted.get("activationFailSafeRequired") is not True:
            raise RuntimeError("Committed activation fail-safe receipt drifted")
        persisted["activationFailSafeRequired"] = False
        self.store.write(persisted)
        transaction.clear()
        transaction.update(persisted)

    def _rearm_after_ambiguous_committed_disarm(
        self,
        armed_authority: dict[str, Any],
        durable_disarmed: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore an exact armed receipt after a disarm write applied then raised."""
        original_plan = self._activation_fail_safe_plan(armed_authority)
        expected_disarmed = json.loads(json.dumps(armed_authority))
        expected_disarmed["activationFailSafeRequired"] = False
        if durable_disarmed != expected_disarmed:
            raise RuntimeError(
                "Committed disarm recovery receipt differs beyond its fail-safe marker"
            )
        rearmed = json.loads(json.dumps(durable_disarmed))
        rearmed["activationFailSafeRequired"] = True
        write_error: Exception | None = None
        try:
            self.store.write(rearmed)
        except Exception as error:
            write_error = error
        persisted = self.store.read()
        if persisted != rearmed:
            failure = RuntimeError(
                "Committed activation fail-safe could not be durably re-armed"
            )
            if write_error is not None:
                raise failure from write_error
            raise failure
        if self._activation_fail_safe_plan(persisted) != original_plan:
            raise RuntimeError(
                "Re-armed activation fail-safe authority changed unexpectedly"
            )
        return persisted

    def _compensate_armed_noncommitted_reentry(
        self, transaction: dict[str, Any],
    ) -> None:
        """Make an interrupted activation safe, then require explicit rollback."""
        self._activation_fail_safe_plan(transaction)
        if self.store.read() != transaction:
            raise RuntimeError(
                "Interrupted activation receipt changed before re-entry compensation"
            )
        try:
            self._disable_uncommitted_managed_jobs_for_activation_failure(
                transaction,
            )
        except Exception as error:
            raise ActivationFailSafeIncomplete(error) from error
        raise RuntimeError(
            "Interrupted OpenClaw integration was safely disabled and requires rollback"
        )

    def _resume_armed_committed_transaction(
        self, transaction: dict[str, Any],
    ) -> dict[str, Any]:
        """Finish or safely roll back a commit that crashed before disarming."""
        armed_authority = json.loads(json.dumps(transaction))
        armed_plan = self._activation_fail_safe_plan(armed_authority)
        try:
            verification = self.verify()
            self._disarm_activation_fail_safe_after_verify(transaction)
            return {
                "status": transaction.get("indexState"),
                "transaction": "already_current",
                **verification,
            }
        except Exception as original_error:
            try:
                latest = self.store.read()
                if latest.get("phase") == "committed" \
                        and latest.get("activationFailSafeRequired") is False:
                    try:
                        verification = self.verify()
                    except Exception as second_verify_error:
                        original_error = second_verify_error
                        try:
                            latest = self._rearm_after_ambiguous_committed_disarm(
                                armed_authority, latest,
                            )
                        except Exception as rearm_error:
                            raise ActivationFailSafeIncomplete(
                                rearm_error,
                            ) from second_verify_error
                    else:
                        return {
                            "status": latest.get("indexState"),
                            "transaction": "already_current",
                            **verification,
                        }
                if latest.get("phase") != "committed" \
                        or latest.get("activationFailSafeRequired") is not True \
                        or self._activation_fail_safe_plan(latest) != armed_plan:
                    raise RuntimeError(
                        "Committed activation fail-safe authority drifted during recovery"
                    )
                latest["failurePhase"] = "committed"
                latest["phase"] = "failed"
                self.store.write(latest)
            except ActivationFailSafeIncomplete:
                raise
            except Exception:
                latest = self.store.read()
                if latest.get("activationFailSafeRequired") is not True:
                    raise ActivationFailSafeIncomplete(
                        RuntimeError(
                            "Committed recovery lost durable activation fail-safe authority"
                        )
                    ) from original_error
            rollback_error: Exception | None = None
            try:
                self._rollback_locked(require_exact_post_config=False)
            except Exception as error:
                rollback_error = error
                try:
                    latest = self.store.read()
                    latest.setdefault("failurePhase", "committed")
                    latest["phase"] = "rollback_failed"
                    self.store.write(latest)
                except Exception:
                    pass
            if rollback_error is not None:
                raise IntegrationRollbackIncomplete(
                    original_error, rollback_error,
                ) from original_error
            raise

    def _integrate_locked(self, runtime_manifest: dict[str, Any]) -> dict[str, Any]:
        prior: dict[str, Any] | None = None
        prior_committed_v3_repair: dict[str, Any] | None = None
        if self.store.manifest_path.is_file() and not self.store.manifest_path.is_symlink():
            existing = self.store.read()
            armed = existing.get("activationFailSafeRequired", False)
            if type(armed) is not bool:
                raise RuntimeError("Activation fail-safe durable marker is malformed")
            if existing.get("phase") == "committed":
                prior = existing
                if existing.get("contractVersion") == INTEGRATION_CONTRACT_VERSION:
                    if armed:
                        return self._resume_armed_committed_transaction(existing)
                    try:
                        return {"status": existing.get("indexState"), "transaction": "already_current", **self.verify()}
                    except Exception:
                        prior_committed_v3_repair = existing
            elif armed:
                self._compensate_armed_noncommitted_reentry(existing)
            elif existing.get("phase") != "rolled_back":
                raise RuntimeError("An unfinished OpenClaw integration transaction requires rollback")
        jobs_before, legacy_before = self._preflight_cron_inventory()
        if prior_committed_v3_repair is not None:
            if "cronContractHashVersion" in prior_committed_v3_repair \
                    or any(
                        key.startswith("cronCommit")
                        for key in prior_committed_v3_repair
                    ):
                self._verify_committed_gemini_receipt(
                    prior_committed_v3_repair, jobs=jobs_before,
                )
            else:
                self._verify_legacy_committed_v3_repair_authority(
                    prior_committed_v3_repair, jobs_before,
                )
        gemini_before = self._owned_gemini_jobs_exact(jobs_before)
        prior_definitions = [
            _job_definition(job) for job in jobs_before
            if job.get("declarationKey") in {
                CRON_DECLARATION_KEY, SNAPSHOT_CRON_DECLARATION_KEY, INITIAL_CRON_DECLARATION_KEY,
            } or any(job.get("id") == legacy.get("id") for legacy in legacy_before)
            or any(job.get("id") == gemini.get("id") for gemini in gemini_before)
        ]
        if any(
            not isinstance(definition.get("name"), str)
            or not definition["name"]
            or definition.get("description") is not None
            and (
                not isinstance(definition.get("description"), str)
                or not definition["description"]
            )
            for definition in prior_definitions
        ):
            raise RuntimeError(
                "Owned cron definition cannot be restored through a unique staging identity"
            )
        target_ids = {
            str(job["id"]) for job in jobs_before
            if job.get("declarationKey") in MANAGED_CRON_KEYS
            or any(job.get("id") == legacy.get("id") for legacy in legacy_before)
            or any(job.get("id") == gemini.get("id") for gemini in gemini_before)
        }
        inventory_hashes_before = self._inventory_hashes(jobs_before)
        unknown_hashes_before = {
            job_id: fingerprint for job_id, fingerprint in inventory_hashes_before.items()
            if job_id not in target_ids
        }
        planned_gemini_disables = [
            {"id": str(job["id"]), "wasEnabled": True}
            for job in gemini_before if job.get("enabled", True) is True
        ]
        quiesced_gemini_hashes: dict[str, str] = {}
        for job in gemini_before:
            expected = json.loads(json.dumps(job))
            expected["enabled"] = False
            quiesced_gemini_hashes[str(job["id"])] = _job_contract_hash(
                expected, include_id=True,
            )
        transaction = self.begin(cron_inventory_hashes=inventory_hashes_before)
        try:
            transaction["previousPhase"] = prior.get("phase") if prior else None
            transaction["previousContractVersion"] = prior.get("contractVersion", 1) if prior else None
            transaction["cronDefinitionsBefore"] = prior_definitions
            transaction["cronContractHashVersion"] = CRON_CONTRACT_HASH_VERSION
            transaction["cronInventoryTotalBefore"] = len(jobs_before)
            transaction["cronInventoryHashesBefore"] = inventory_hashes_before
            transaction["cronUnknownHashesBefore"] = unknown_hashes_before
            transaction["cronPreservedGeminiHashesAfterQuiesce"] = quiesced_gemini_hashes
            transaction["cronTargetIdsBefore"] = sorted(target_ids)
            transaction["cronLegacyRemoveIdsBefore"] = sorted(
                str(job["id"]) for job in legacy_before
            )
            transaction["managedCronIdsAfter"] = []
            transaction["cronStagingIntents"] = {}
            transaction["cronRestoreIntents"] = {}
            transaction["restoredCronIdsByDeclaration"] = {}
            transaction["cronMutationStarted"] = False
            transaction["runtimeMutationStarted"] = False
            transaction["pluginMutationStarted"] = False
            transaction["configMutationStarted"] = False
            transaction["skillMutationStarted"] = False
            transaction["projectRuntimeMutationStarted"] = False
            transaction["plistMutationStarted"] = False
            transaction["launchdMutationStarted"] = False
            transaction["disabledGeminiJobs"] = planned_gemini_disables
            transaction["ownership"] = self._ownership_payload()
            transaction["runtimePort"] = int(runtime_manifest["runtimePort"])
            transaction["snapshotRootCreatePlanned"] = not self.snapshot_root.exists()
            transaction["snapshotRootCreated"] = False
            transaction["projectCreatePlanned"] = not transaction.get("projectExisted", False)
            transaction["projectCreated"] = False
            transaction["phase"] = "preflight_complete"
            self.store.write(transaction)
            project_probe_target = (
                self.paths.project_root
                if transaction.get("projectExisted") is True
                else self.paths.project_root.parent
            )
            snapshot_probe_target = (
                self.snapshot_root
                if transaction["snapshotRootCreatePlanned"] is False
                else self.snapshot_root.parent
            )
            project_probe_parent = self._nearest_existing_capability_parent(
                project_probe_target
            )
            snapshot_probe_parent = self._nearest_existing_capability_parent(
                snapshot_probe_target
            )
            self._probe_atomic_publication_capability(
                transaction,
                parent=project_probe_parent,
                receipt_prefix="projectParentCapabilityProbe",
            )
            if Path(os.path.abspath(snapshot_probe_parent)) != Path(
                os.path.abspath(project_probe_parent)
            ):
                self._probe_atomic_publication_capability(
                    transaction,
                    parent=snapshot_probe_parent,
                    receipt_prefix="snapshotParentCapabilityProbe",
                )
            transaction["cronMutationStarted"] = True
            transaction["phase"] = "quiescing"
            self.store.write(transaction)
            transaction["quiescedCronIds"] = self._quiesce_prior_jobs(
                jobs_before, target_ids, inventory_hashes_before,
            )
            created_snapshot_root = self._prepare_snapshot_root(transaction)
            if created_snapshot_root != transaction["snapshotRootCreatePlanned"]:
                raise RuntimeError("Snapshot root identity changed during integration preflight")
            transaction["phase"] = "quiesced"
            self.store.write(transaction)

            def checkpoint_quiescence_lock(lock_receipt: dict[str, Any]) -> None:
                for key, value in lock_receipt.items():
                    if key in transaction and transaction[key] != value:
                        if key in {"indexLockPublished", "snapshotLockPublished"} \
                                and transaction[key] is False and value is True:
                            pass
                        else:
                            raise RuntimeError("Quiescence lock receipt attempted to change identity")
                    transaction[key] = value
                self.store.write(transaction)

            with self._runtime_quiescence_guard(
                checkpoint=checkpoint_quiescence_lock,
            ) as lock_receipt:
                transaction.update({
                    key: value for key, value in lock_receipt.items()
                    if key not in {
                        "persisted", "indexLockPersisted", "snapshotLockPersisted",
                    }
                })
                self.store.write(transaction)
                lock_receipt["persisted"] = True
                transaction["runtimeMutationStarted"] = True
                transaction["phase"] = "staging"
                self.store.write(transaction)
                if transaction["projectCreatePlanned"]:
                    self._prepare_project_root(transaction)
                actual_project_created = self.bootstrap_project(runtime_manifest)
                if actual_project_created != transaction["projectCreated"]:
                    raise RuntimeError("Qwen project creation state changed during integration")
                transaction["projectBootstrapped"] = actual_project_created
                if transaction["projectCreated"]:
                    project_meta = self.paths.project_root.lstat()
                    if not stat.S_ISDIR(project_meta.st_mode) or project_meta.st_uid != os.getuid() \
                            or project_meta.st_mode & 0o077 \
                            or (project_meta.st_dev, project_meta.st_ino) != (
                                transaction["projectRootDev"], transaction["projectRootIno"],
                            ):
                        raise RuntimeError("Fresh Qwen project identity changed during bootstrap")
                if not actual_project_created:
                    self.synchronize_project_runtime(transaction)
                self.store.write(transaction)

                self.configure_openclaw(self._allowed_projects(), transaction=transaction)
                self._checkpoint_mutation(transaction, "plistMutationStarted")
                self.install_launchd_plist(runtime_manifest)
                transaction["ownedAssets"] = [
                    PLUGIN_ID, SKILL_ID, LAUNCHD_LABEL, CRON_DECLARATION_KEY,
                    SNAPSHOT_CRON_DECLARATION_KEY, HEALTH_RECEIPT_SCHEMA,
                ]
                transaction["phase"] = "activating"
                self.store.write(transaction)

                self._checkpoint_mutation(transaction, "launchdMutationStarted")
                self.activate_launchd()
                transaction["cronReplaceIdsBefore"] = sorted(
                    str(job["id"]) for job in jobs_before
                    if job.get("declarationKey") in MANAGED_CRON_KEYS
                )
                transaction["phase"] = "replacing_managed_cron"
                self.store.write(transaction)
                transaction["removedManagedCronIdsBeforeAdd"] = (
                    self._remove_prior_managed_jobs_for_replacement(
                        jobs_before, target_ids, inventory_hashes_before,
                    )
                )
                transaction["phase"] = "staging_managed_cron"
                self.store.write(transaction)
                transaction["cronId"] = self._apply_managed_spec(
                    self._incremental_spec(), transaction,
                )
                transaction["snapshotCronId"] = self._apply_managed_spec(
                    self._snapshot_spec(), transaction,
                )
                self._verify_recurring_specs(enabled=False)
                self.store.write(transaction)
                persisted = self.store.read()
                expected_legacy_ids = sorted(str(job["id"]) for job in legacy_before)
                if persisted.get("cronLegacyRemoveIdsBefore") != expected_legacy_ids:
                    raise RuntimeError("Legacy cron removal durable authority drifted")
                legacy_removal_inventory = self._inventory()
                if legacy_before:
                    self._verify_pre_legacy_removal_inventory(
                        persisted, legacy_removal_inventory,
                    )
                legacy_current_by_id = {
                    str(job["id"]): job for job in legacy_removal_inventory
                }
                for legacy in legacy_before:
                    job_id = str(legacy["id"])
                    expected = json.loads(json.dumps(legacy))
                    expected["enabled"] = False
                    current_legacy = legacy_current_by_id.get(job_id)
                    if current_legacy is None or _job_contract_hash(
                        current_legacy, include_id=True,
                    ) != _job_contract_hash(expected, include_id=True):
                        raise RuntimeError(
                            "Legacy snapshot identity was reused or drifted before removal"
                        )
                self._remove_cron_ids_with_snapshot_guard(
                    legacy_removal_inventory,
                    {str(legacy["id"]) for legacy in legacy_before},
                )
                if legacy_before:
                    current_ids = {str(job["id"]) for job in self._inventory()}
                    if any(str(legacy["id"]) in current_ids for legacy in legacy_before):
                        raise RuntimeError("Legacy snapshot declaration remained after explicit migration")
                    transaction["removedLegacySnapshotJobs"] = [str(job["id"]) for job in legacy_before]
                current_by_id = {str(job["id"]): job for job in self._inventory()}
                if any(current_by_id.get(item["id"], {}).get("enabled", True) is not False
                       for item in planned_gemini_disables):
                    raise RuntimeError("Gemini rollback declarations did not remain quiesced")
                transaction["indexState"], transaction["initialIndexJobId"] = (
                    self.mark_ready_or_schedule_build(transaction)
                )
                self.store.write(transaction)
                self._verify_success_cron_inventory(
                    transaction,
                    recurring_enabled=False,
                    initial_enabled=False,
                )
                self.cli.run(["config", "validate", "--json"])
                config = Path(transaction["configPath"])
                transaction["postConfigSha256"] = self._sha256_config(config)
                transaction["phase"] = "restarting_gateway"
                self.store.write(transaction)
                self.cli.run(["gateway", "restart", "--safe", "--json"], timeout=300)
                transaction["activationFailSafeRequired"] = True
                transaction["activationFailSafeDisabledCronIds"] = []
                transaction["activationFailSafeComplete"] = False
                transaction["phase"] = "activation_pending"
                self.store.write(transaction)

            self._enable_recurring_jobs([transaction["cronId"], transaction["snapshotCronId"]])
            if transaction["initialIndexJobId"]:
                self._enable_initial_job(transaction["initialIndexJobId"])
                self._write_health_receipt(event="initial", status="pending")
            else:
                self._write_health_receipt(event="incremental", status="ok")
            transaction["healthReceiptSha256"] = sha256_file(self.health_receipt_path)
            self.store.write(transaction)
            self._verify_activation_pending(transaction)
            transaction["cronCommitInventoryHashes"] = self._verify_success_cron_inventory(
                transaction,
                recurring_enabled=True,
                initial_enabled=True,
            )
            transaction["cronCommitUnknownHashes"] = dict(
                transaction["cronUnknownHashesBefore"]
            )
            transaction["cronCommitGeminiHashes"] = dict(
                transaction["cronPreservedGeminiHashesAfterQuiesce"]
            )
            transaction["cronCommitTopologyVerified"] = True
            transaction["phase"] = "commit_closeout_pending"
            self.store.write(transaction)
            self._verify_commit_cron_receipt(transaction)
            transaction["phase"] = "committed"
            self.store.write(transaction)
            verification = self.verify()
            self._disarm_activation_fail_safe_after_verify(transaction)
            action = "upgraded" if prior else "committed"
            return {"status": transaction["indexState"], "transaction": action, **verification}
        except Exception as original_error:
            try:
                transaction["failurePhase"] = transaction.get("phase")
                transaction["phase"] = "failed"
                self.store.write(transaction)
            except Exception:
                pass
            rollback_error: Exception | None = None
            try:
                self._rollback_locked(require_exact_post_config=False)
            except Exception as error:
                rollback_error = error
                try:
                    latest = self.store.read()
                    latest["phase"] = "rollback_failed"
                    self.store.write(latest)
                except Exception:
                    pass
            if rollback_error is not None:
                raise IntegrationRollbackIncomplete(original_error, rollback_error) from original_error
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

    def _snapshot_root_from_transaction(self, transaction: dict[str, Any]) -> Path:
        if transaction.get("contractVersion") != INTEGRATION_CONTRACT_VERSION:
            raise RuntimeError(
                "Legacy rollback receipt lacks exact snapshot-root ownership"
            )
        ownership = transaction.get("ownership")
        if not isinstance(ownership, dict) or ownership.get("schema") != OWNERSHIP_SCHEMA:
            raise RuntimeError("Rollback snapshot ownership is missing or malformed")
        stored = ownership.get("snapshotRoot")
        if not isinstance(stored, str) or not stored:
            raise RuntimeError("Rollback snapshot ownership is missing or malformed")
        raw = Path(stored).expanduser()
        if not raw.is_absolute():
            raise RuntimeError("Rollback snapshot ownership must be absolute")
        root = Path(os.path.abspath(raw))
        _assert_no_symlink_components(root)
        _assert_specific_child(root, self.paths.home, "rollback snapshot root")
        if root != self.snapshot_root:
            raise RuntimeError("Rollback snapshot root does not match transaction ownership")
        project = self.paths.project_root.resolve(strict=False)
        resolved = root.resolve(strict=False)
        if resolved == project or project in resolved.parents:
            raise RuntimeError("Rollback snapshot root is inside the live Qwen project")
        return root

    def _project_root_from_transaction(self, transaction: dict[str, Any]) -> Path:
        if transaction.get("contractVersion") != INTEGRATION_CONTRACT_VERSION:
            raise RuntimeError(
                "Legacy rollback receipt lacks exact project-root ownership"
            )
        ownership = transaction.get("ownership")
        if not isinstance(ownership, dict) or ownership.get("schema") != OWNERSHIP_SCHEMA:
            raise RuntimeError("Rollback project ownership is missing or malformed")
        stored = ownership.get("projectRoot")
        if not isinstance(stored, str) or not stored:
            raise RuntimeError("Rollback project ownership is missing or malformed")
        raw = Path(stored).expanduser()
        if not raw.is_absolute():
            raise RuntimeError("Rollback project ownership must be absolute")
        root = Path(os.path.abspath(raw))
        _assert_no_symlink_components(root)
        _assert_specific_child(root, self.paths.workspace, "rollback project root")
        if root != self.paths.project_root:
            raise RuntimeError("Rollback project root does not match transaction ownership")
        return root

    @staticmethod
    def _legacy_unverified_created_root(
        transaction: dict[str, Any], *, prefix: str, planned_key: str, created_key: str,
    ) -> bool:
        if transaction.get("contractVersion") != INTEGRATION_CONTRACT_VERSION \
                or transaction.get(created_key) is not True:
            return False
        fields = (
            planned_key,
            f"{prefix}StageName",
            f"{prefix}StageDev",
            f"{prefix}StageIno",
            f"{prefix}Published",
            f"{prefix}Dev",
            f"{prefix}Ino",
        )
        return all(field not in transaction for field in fields)

    def _remove_staged_runtime_lock(
        self, transaction: dict[str, Any], *, prefix: str, parent: Path,
        final_name: str, stage_pattern: re.Pattern[str], directory: bool,
    ) -> bool:
        stage_name = transaction.get(f"{prefix}StageName")
        stage_dev = transaction.get(f"{prefix}StageDev")
        stage_ino = transaction.get(f"{prefix}StageIno")
        published = transaction.get(f"{prefix}Published")
        fields = (stage_name, stage_dev, stage_ino, published)
        if all(value is None for value in fields):
            return False
        if not isinstance(stage_name, str) or stage_pattern.fullmatch(stage_name) is None \
                or type(stage_dev) is not int or type(stage_ino) is not int \
                or type(published) is not bool:
            raise RuntimeError("Staged runtime lock receipt is missing or malformed")
        if not os.path.lexists(parent):
            return True
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        expected = (stage_dev, stage_ino)
        try:
            parent_meta = os.fstat(parent_fd)
            if not stat.S_ISDIR(parent_meta.st_mode) or parent_meta.st_uid != os.getuid() \
                    or parent_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError("Runtime lock parent is unsafe during rollback")

            def metadata_for(name: str) -> os.stat_result | None:
                try:
                    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None

            stage_meta = metadata_for(stage_name)
            final_meta = metadata_for(final_name)
            if stage_meta is not None:
                if directory:
                    self._validate_index_lock(stage_meta)
                else:
                    self._validate_snapshot_lock(stage_meta)
                if (stage_meta.st_dev, stage_meta.st_ino) != expected:
                    raise RuntimeError("Staged runtime lock changed before rollback")
                if final_meta is not None:
                    raise RuntimeError("Staged runtime lock collided with the final path")
                self._quarantine_transaction_path(
                    transaction,
                    receipt_prefix=prefix,
                    parent_fd=parent_fd,
                    name=stage_name,
                    expected_identity=expected,
                    directory=directory,
                )
                return True
            if final_meta is not None and (final_meta.st_dev, final_meta.st_ino) == expected:
                self._quarantine_transaction_path(
                    transaction,
                    receipt_prefix=prefix,
                    parent_fd=parent_fd,
                    name=final_name,
                    expected_identity=expected,
                    directory=directory,
                )
                return True
            if published is True or transaction.get(f"{prefix}Created") is True:
                if final_meta is not None:
                    raise RuntimeError("Published runtime lock changed before rollback")
            return True
        finally:
            os.close(parent_fd)

    def _quarantine_staged_root(
        self, transaction: dict[str, Any], *, prefix: str, created_key: str,
        parent: Path, final_name: str, stage_pattern: re.Pattern[str],
    ) -> bool:
        stage_name = transaction.get(f"{prefix}StageName")
        stage_dev = transaction.get(f"{prefix}StageDev")
        stage_ino = transaction.get(f"{prefix}StageIno")
        published = transaction.get(f"{prefix}Published")
        fields = (stage_name, stage_dev, stage_ino, published)
        if all(value is None for value in fields):
            return False
        if not isinstance(stage_name, str) or stage_pattern.fullmatch(stage_name) is None \
                or type(stage_dev) is not int or type(stage_ino) is not int \
                or type(published) is not bool:
            raise RuntimeError("Staged managed root receipt is missing or malformed")
        if not os.path.lexists(parent):
            return True
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        expected = (stage_dev, stage_ino)
        try:
            self._validate_private_directory(os.fstat(parent_fd))

            def metadata_for(name: str) -> os.stat_result | None:
                try:
                    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None

            stage_meta = metadata_for(stage_name)
            final_meta = metadata_for(final_name)
            if stage_meta is not None:
                self._validate_restricted_directory(stage_meta)
                if (stage_meta.st_dev, stage_meta.st_ino) != expected:
                    raise RuntimeError("Staged managed root changed before rollback")
                if final_meta is not None:
                    raise RuntimeError("Staged managed root collided with the final path")
                self._quarantine_transaction_path(
                    transaction,
                    receipt_prefix=prefix,
                    parent_fd=parent_fd,
                    name=stage_name,
                    expected_identity=expected,
                    directory=True,
                )
                return True
            if transaction.get(created_key) is True:
                return True
            if final_meta is not None and (final_meta.st_dev, final_meta.st_ino) == expected:
                self._quarantine_transaction_path(
                    transaction,
                    receipt_prefix=prefix,
                    parent_fd=parent_fd,
                    name=final_name,
                    expected_identity=expected,
                    directory=True,
                )
                return True
            if final_meta is not None:
                raise RuntimeError("Published managed root changed before rollback")
            return True
        finally:
            os.close(parent_fd)

    def _preflight_created_snapshot_artifacts(
        self, transaction: dict[str, Any],
    ) -> None:
        """Validate every cleanup capability and inode receipt without mutating state."""
        project_root = self._project_root_from_transaction(transaction)
        snapshot_root = self._snapshot_root_from_transaction(transaction)

        def optional_metadata(parent_fd: int, name: str) -> os.stat_result | None:
            try:
                return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None

        def validate_probe(prefix: str, expected_parent: Path) -> None:
            stage_name = transaction.get(f"{prefix}StageName")
            final_name = transaction.get(f"{prefix}FinalName")
            stage_dev = transaction.get(f"{prefix}StageDev")
            stage_ino = transaction.get(f"{prefix}StageIno")
            published = transaction.get(f"{prefix}Published")
            parent_value = transaction.get(f"{prefix}Parent")
            fields = (stage_name, final_name, stage_dev, stage_ino, published, parent_value)
            if all(value is None for value in fields):
                if any(transaction.get(f"{prefix}{suffix}") is not None for suffix in (
                    "QuarantineName", "Quarantined", "QuarantinePurged", "Preserved",
                )):
                    raise RuntimeError("Atomic publication capability receipt is incomplete")
                return
            if not isinstance(stage_name, str) \
                    or CAPABILITY_PROBE_STAGE_RE.fullmatch(stage_name) is None \
                    or not isinstance(final_name, str) \
                    or CAPABILITY_PROBE_FINAL_RE.fullmatch(final_name) is None \
                    or type(stage_dev) is not int or type(stage_ino) is not int \
                    or type(published) is not bool or not isinstance(parent_value, str):
                raise RuntimeError("Atomic publication capability receipt is malformed")
            for suffix in ("Quarantined", "QuarantinePurged", "Preserved"):
                value = transaction.get(f"{prefix}{suffix}")
                if value is not None and type(value) is not bool:
                    raise RuntimeError("Atomic publication capability state is malformed")
            parent = Path(os.path.abspath(Path(parent_value).expanduser()))
            if parent != Path(os.path.abspath(expected_parent)):
                raise RuntimeError("Atomic publication capability parent changed")
            quarantine_name = transaction.get(f"{prefix}QuarantineName")
            if quarantine_name is not None and (
                not isinstance(quarantine_name, str)
                or QUARANTINE_RE.fullmatch(quarantine_name) is None
            ):
                raise RuntimeError("Atomic publication capability quarantine receipt is malformed")
            if not os.path.lexists(parent):
                return
            expected = (stage_dev, stage_ino)
            with self._open_private_directory(parent) as parent_fd:
                matches = 0
                for name in (stage_name, final_name, quarantine_name):
                    if not isinstance(name, str):
                        continue
                    metadata = optional_metadata(parent_fd, name)
                    if metadata is None:
                        continue
                    self._validate_restricted_directory(metadata)
                    if (metadata.st_dev, metadata.st_ino) != expected:
                        raise RuntimeError("Atomic publication capability artifact changed")
                    matches += 1
                if matches > 1:
                    raise RuntimeError(
                        "Atomic publication capability artifact exists at multiple paths"
                    )

        def validate_staged_path(
            *, prefix: str, parent: Path, final_name: str,
            stage_pattern: re.Pattern[str], directory: bool, root: bool = False,
            created_key: str,
        ) -> bool:
            stage_name = transaction.get(f"{prefix}StageName")
            stage_dev = transaction.get(f"{prefix}StageDev")
            stage_ino = transaction.get(f"{prefix}StageIno")
            published = transaction.get(f"{prefix}Published")
            fields = (stage_name, stage_dev, stage_ino, published)
            quarantine_name = transaction.get(f"{prefix}QuarantineName")
            if all(value is None for value in fields):
                if quarantine_name is not None \
                        or transaction.get(f"{prefix}Quarantined") is not None \
                        or transaction.get(f"{prefix}QuarantinePurged") is not None:
                    raise RuntimeError("Staged cleanup receipt is incomplete")
                return False
            if not isinstance(stage_name, str) or stage_pattern.fullmatch(stage_name) is None \
                    or type(stage_dev) is not int or type(stage_ino) is not int \
                    or type(published) is not bool:
                raise RuntimeError("Staged cleanup receipt is missing or malformed")
            if quarantine_name is not None and (
                not isinstance(quarantine_name, str)
                or QUARANTINE_RE.fullmatch(quarantine_name) is None
            ):
                raise RuntimeError("Staged cleanup quarantine receipt is malformed")
            for suffix in ("Quarantined", "QuarantinePurged"):
                value = transaction.get(f"{prefix}{suffix}")
                if value is not None and type(value) is not bool:
                    raise RuntimeError("Staged cleanup state is malformed")
            if not os.path.lexists(parent):
                return True
            expected = (stage_dev, stage_ino)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parent_meta = os.fstat(parent_fd)
                if root:
                    self._validate_private_directory(parent_meta)
                elif not stat.S_ISDIR(parent_meta.st_mode) \
                        or parent_meta.st_uid != os.getuid() \
                        or parent_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise RuntimeError("Runtime lock parent is unsafe during rollback")
                stage_meta = optional_metadata(parent_fd, stage_name)
                final_meta = optional_metadata(parent_fd, final_name)
                quarantine_meta = optional_metadata(parent_fd, quarantine_name) \
                    if isinstance(quarantine_name, str) else None

                def validate_owned(metadata: os.stat_result) -> None:
                    if root:
                        self._validate_restricted_directory(metadata)
                    elif directory:
                        self._validate_index_lock(metadata)
                    else:
                        self._validate_snapshot_lock(metadata)
                    if (metadata.st_dev, metadata.st_ino) != expected:
                        label = "managed root" if root else "runtime lock"
                        raise RuntimeError(
                            f"Staged {label} changed before rollback"
                        )

                if stage_meta is not None:
                    validate_owned(stage_meta)
                    if final_meta is not None:
                        raise RuntimeError("Staged cleanup artifact collided with the final path")
                final_owned = final_meta is not None \
                    and (final_meta.st_dev, final_meta.st_ino) == expected
                if final_owned:
                    validate_owned(final_meta)
                elif final_meta is not None and (
                    published is True or transaction.get(created_key) is True
                ):
                    raise RuntimeError("Published cleanup artifact changed before rollback")
                if quarantine_meta is not None:
                    validate_owned(quarantine_meta)
                if sum((stage_meta is not None, final_owned, quarantine_meta is not None)) > 1:
                    raise RuntimeError("Cleanup artifact exists at multiple paths")
            finally:
                os.close(parent_fd)
            return True

        project_probe_target = (
            project_root if transaction.get("projectExisted") is True else project_root.parent
        )
        snapshot_probe_target = (
            snapshot_root
            if transaction.get("snapshotRootCreatePlanned") is False
            else snapshot_root.parent
        )
        project_probe_parent = self._capability_parent_from_transaction(
            transaction,
            receipt_prefix="projectParentCapabilityProbe",
            target_parent=project_probe_target,
        )
        snapshot_probe_parent = self._capability_parent_from_transaction(
            transaction,
            receipt_prefix="snapshotParentCapabilityProbe",
            target_parent=snapshot_probe_target,
        )
        validate_probe("projectParentCapabilityProbe", project_probe_parent)
        validate_probe("snapshotParentCapabilityProbe", snapshot_probe_parent)

        validate_staged_path(
            prefix="projectRoot", parent=project_root.parent,
            final_name=project_root.name, stage_pattern=PROJECT_ROOT_STAGE_RE,
            directory=True, root=True, created_key="projectCreated",
        )
        if transaction.get("projectCreated") is True and os.path.lexists(project_root):
            expected_dev = transaction.get("projectRootDev")
            expected_ino = transaction.get("projectRootIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                if not self._legacy_unverified_created_root(
                    transaction,
                    prefix="projectRoot",
                    planned_key="projectCreatePlanned",
                    created_key="projectCreated",
                ):
                    raise RuntimeError("Created Qwen project identity is missing")
            else:
                with self._open_private_directory(project_root.parent) as parent_fd:
                    metadata = optional_metadata(parent_fd, project_root.name)
                    if metadata is not None:
                        self._validate_restricted_directory(metadata)
                        if (metadata.st_dev, metadata.st_ino) != (expected_dev, expected_ino):
                            raise RuntimeError("Created Qwen project identity changed")
        index_staged = validate_staged_path(
            prefix="indexLock", parent=project_root / "data", final_name="index.lock",
            stage_pattern=INDEX_LOCK_STAGE_RE, directory=True,
            created_key="indexLockCreated",
        )
        if not index_staged and transaction.get("indexLockCreated") is True:
            expected_dev = transaction.get("indexLockDev")
            expected_ino = transaction.get("indexLockIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                raise RuntimeError("Created index lock identity is missing")
            data = project_root / "data"
            try:
                with self._open_private_directory(data) as data_fd:
                    metadata = optional_metadata(data_fd, "index.lock")
                    if metadata is not None:
                        self._validate_index_lock(metadata)
                        if (metadata.st_dev, metadata.st_ino) != (expected_dev, expected_ino):
                            raise RuntimeError("Created index lock changed before rollback")
            except OSError as error:
                raise RuntimeError("Created index lock could not be safely inspected") from error

        snapshot_staged = validate_staged_path(
            prefix="snapshotLock", parent=snapshot_root,
            final_name=".snapshot-run.lock", stage_pattern=SNAPSHOT_LOCK_STAGE_RE,
            directory=False, created_key="snapshotLockCreated",
        )
        if not snapshot_staged and transaction.get("snapshotLockCreated") is True:
            expected_dev = transaction.get("snapshotLockDev")
            expected_ino = transaction.get("snapshotLockIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                raise RuntimeError("Created snapshot lock identity is missing")
            if not os.path.lexists(snapshot_root):
                if transaction.get("snapshotRootCreated") is not True:
                    raise RuntimeError("Pre-existing snapshot root disappeared before rollback")
            else:
                with self._open_private_directory(snapshot_root) as root_fd:
                    metadata = optional_metadata(root_fd, ".snapshot-run.lock")
                    if metadata is not None:
                        self._validate_snapshot_lock(metadata)
                        if (metadata.st_dev, metadata.st_ino) != (expected_dev, expected_ino):
                            raise RuntimeError("Created snapshot lock changed before rollback")

        validate_staged_path(
            prefix="snapshotRoot", parent=snapshot_root.parent,
            final_name=snapshot_root.name, stage_pattern=SNAPSHOT_ROOT_STAGE_RE,
            directory=True, root=True, created_key="snapshotRootCreated",
        )
        if transaction.get("snapshotRootCreated") is True \
                and os.path.lexists(snapshot_root):
            expected_dev = transaction.get("snapshotRootDev")
            expected_ino = transaction.get("snapshotRootIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                if not self._legacy_unverified_created_root(
                    transaction,
                    prefix="snapshotRoot",
                    planned_key="snapshotRootCreatePlanned",
                    created_key="snapshotRootCreated",
                ):
                    raise RuntimeError("Created snapshot root identity is missing")
            else:
                with self._open_private_directory(snapshot_root.parent) as parent_fd:
                    metadata = optional_metadata(parent_fd, snapshot_root.name)
                    if metadata is not None:
                        self._validate_restricted_directory(metadata)
                        if (metadata.st_dev, metadata.st_ino) != (expected_dev, expected_ino):
                            raise RuntimeError("Created snapshot root is unsafe during rollback")

    def _remove_created_snapshot_artifacts(self, transaction: dict[str, Any]) -> None:
        self._preflight_created_snapshot_artifacts(transaction)
        project_root = self._project_root_from_transaction(transaction)
        project_probe_target = (
            project_root if transaction.get("projectExisted") is True else project_root.parent
        )
        snapshot_root = self._snapshot_root_from_transaction(transaction)
        snapshot_probe_target = (
            snapshot_root
            if transaction.get("snapshotRootCreatePlanned") is False
            else snapshot_root.parent
        )
        project_probe_parent = self._capability_parent_from_transaction(
            transaction,
            receipt_prefix="projectParentCapabilityProbe",
            target_parent=project_probe_target,
        )
        snapshot_probe_parent = self._capability_parent_from_transaction(
            transaction,
            receipt_prefix="snapshotParentCapabilityProbe",
            target_parent=snapshot_probe_target,
        )
        self._recover_capability_probe(
            transaction,
            receipt_prefix="projectParentCapabilityProbe",
            expected_parent=project_probe_parent,
        )
        self._recover_capability_probe(
            transaction,
            receipt_prefix="snapshotParentCapabilityProbe",
            expected_parent=snapshot_probe_parent,
        )
        self._quarantine_staged_root(
            transaction,
            prefix="projectRoot",
            created_key="projectCreated",
            parent=project_root.parent,
            final_name=project_root.name,
            stage_pattern=PROJECT_ROOT_STAGE_RE,
        )
        index_staged = self._remove_staged_runtime_lock(
            transaction,
            prefix="indexLock",
            parent=project_root / "data",
            final_name="index.lock",
            stage_pattern=INDEX_LOCK_STAGE_RE,
            directory=True,
        )
        if not index_staged and transaction.get("indexLockCreated") is True:
            expected_dev = transaction.get("indexLockDev")
            expected_ino = transaction.get("indexLockIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                raise RuntimeError("Created index lock identity is missing")
            data = project_root / "data"
            data_fd: int | None = None
            try:
                data_fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                data_meta = os.fstat(data_fd)
                if not stat.S_ISDIR(data_meta.st_mode) or data_meta.st_uid != os.getuid() \
                        or data_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise RuntimeError("Qwen data directory is unsafe during rollback")
                try:
                    lock_meta = os.stat("index.lock", dir_fd=data_fd, follow_symlinks=False)
                except FileNotFoundError:
                    lock_meta = None
                if lock_meta is not None:
                    if (lock_meta.st_dev, lock_meta.st_ino) != (expected_dev, expected_ino) \
                            or not stat.S_ISDIR(lock_meta.st_mode) or lock_meta.st_uid != os.getuid() \
                            or lock_meta.st_mode & 0o077:
                        raise RuntimeError("Created index lock changed before rollback")
                    self._quarantine_transaction_path(
                        transaction,
                        receipt_prefix="indexLock",
                        parent_fd=data_fd,
                        name="index.lock",
                        expected_identity=(expected_dev, expected_ino),
                        directory=True,
                    )
            except OSError as error:
                raise RuntimeError("Created index lock could not be safely removed") from error
            finally:
                if data_fd is not None:
                    os.close(data_fd)

        root = snapshot_root
        snapshot_staged = self._remove_staged_runtime_lock(
            transaction,
            prefix="snapshotLock",
            parent=root,
            final_name=".snapshot-run.lock",
            stage_pattern=SNAPSHOT_LOCK_STAGE_RE,
            directory=False,
        )
        if not snapshot_staged and transaction.get("snapshotLockCreated") is True:
            expected_dev = transaction.get("snapshotLockDev")
            expected_ino = transaction.get("snapshotLockIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                raise RuntimeError("Created snapshot lock identity is missing")
            if not os.path.lexists(root):
                if transaction.get("snapshotRootCreated") is True:
                    root = self._snapshot_root_from_transaction(transaction)
                else:
                    raise RuntimeError("Pre-existing snapshot root disappeared before rollback")
            else:
                root_fd: int | None = None
                try:
                    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    root_meta = os.fstat(root_fd)
                    if not stat.S_ISDIR(root_meta.st_mode) or root_meta.st_uid != os.getuid() \
                            or root_meta.st_mode & 0o077:
                        raise RuntimeError("Created snapshot root is unsafe during rollback")
                    try:
                        lock_meta = os.stat(
                            ".snapshot-run.lock", dir_fd=root_fd, follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        lock_meta = None
                    if lock_meta is not None:
                        if (lock_meta.st_dev, lock_meta.st_ino) != (expected_dev, expected_ino) \
                                or not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_nlink != 1 \
                                or lock_meta.st_uid != os.getuid() or lock_meta.st_mode & 0o077 \
                                or lock_meta.st_size != 0:
                            raise RuntimeError("Created snapshot lock changed before rollback")
                        self._quarantine_transaction_path(
                            transaction,
                            receipt_prefix="snapshotLock",
                            parent_fd=root_fd,
                            name=".snapshot-run.lock",
                            expected_identity=(expected_dev, expected_ino),
                            directory=False,
                        )
                except OSError as error:
                    raise RuntimeError("Created snapshot lock could not be safely removed") from error
                finally:
                    if root_fd is not None:
                        os.close(root_fd)
        self._quarantine_staged_root(
            transaction,
            prefix="snapshotRoot",
            created_key="snapshotRootCreated",
            parent=root.parent,
            final_name=root.name,
            stage_pattern=SNAPSHOT_ROOT_STAGE_RE,
        )
        if transaction.get("snapshotRootCreated") is True:
            if not os.path.lexists(root):
                return
            expected_dev = transaction.get("snapshotRootDev")
            expected_ino = transaction.get("snapshotRootIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                if self._legacy_unverified_created_root(
                    transaction,
                    prefix="snapshotRoot",
                    planned_key="snapshotRootCreatePlanned",
                    created_key="snapshotRootCreated",
                ):
                    transaction["legacyUnverifiedSnapshotRootPreserved"] = True
                    self.store.write(transaction)
                    return
                raise RuntimeError("Created snapshot root identity is missing")
            with self._open_private_directory(root.parent) as parent_fd:
                try:
                    metadata = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                        or metadata.st_mode & 0o077 \
                        or (metadata.st_dev, metadata.st_ino) != (expected_dev, expected_ino):
                    raise RuntimeError("Created snapshot root is unsafe during rollback")
                self._quarantine_transaction_path(
                    transaction,
                    receipt_prefix="snapshotRoot",
                    parent_fd=parent_fd,
                    name=root.name,
                    expected_identity=(expected_dev, expected_ino),
                    directory=True,
                )

    def _legacy_v3_removable_cron_ids(
        self,
        transaction: dict[str, Any],
        current_jobs: list[dict[str, Any]],
        prior_definitions: list[dict[str, Any]],
    ) -> tuple[set[str], dict[str, str]]:
        """Recover pre-intent contract-v3 receipts using exact, bounded ID authority."""
        target_ids = transaction.get("cronTargetIdsBefore", [])
        managed_ids = transaction.get("managedCronIdsAfter", [])
        inventory_before = transaction.get("cronInventoryHashesBefore")
        unknown_before = transaction.get("cronUnknownHashesBefore")
        if not isinstance(target_ids, list) or not isinstance(managed_ids, list) \
                or any(
                    not isinstance(value, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(value)
                    for value in [*target_ids, *managed_ids]
                ) \
                or len(target_ids) != len(set(target_ids)) \
                or len(managed_ids) != len(set(managed_ids)) \
                or not isinstance(inventory_before, dict) \
                or not isinstance(unknown_before, dict) \
                or any(
                    not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    for receipt in (inventory_before, unknown_before)
                    for job_id, fingerprint in receipt.items()
                ):
            raise RuntimeError("Legacy v3 rollback cron receipt is malformed")
        definitions_by_id: dict[str, dict[str, Any]] = {}
        for definition in prior_definitions:
            job_id = definition.get("id")
            if not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id) \
                    or job_id in definitions_by_id:
                raise RuntimeError("Legacy v3 rollback definition identity is malformed")
            definitions_by_id[job_id] = definition
        target_set = set(target_ids)
        managed_set = set(managed_ids)
        if target_set != set(definitions_by_id) \
                or target_set & managed_set \
                or managed_set & set(inventory_before) \
                or set(inventory_before) != target_set | set(unknown_before) \
                or set(unknown_before) & target_set:
            raise RuntimeError("Legacy v3 rollback cron authority is inconsistent")
        for job_id, definition in definitions_by_id.items():
            expected = _legacy_v3_job_contract_hash(definition, include_id=True)
            if inventory_before.get(job_id) != expected:
                raise RuntimeError("Legacy v3 rollback definition fingerprint drifted")

        current_by_id = {str(job["id"]): job for job in current_jobs}
        upgraded_unknown_hashes: dict[str, str] = {}
        for job_id, legacy_fingerprint in unknown_before.items():
            job = current_by_id.get(job_id)
            if job is None or _legacy_v3_job_contract_hash(
                job, include_id=True,
            ) != legacy_fingerprint:
                raise RuntimeError("Legacy v3 unknown cron receipt drifted")
            upgraded_unknown_hashes[job_id] = _job_contract_hash(
                job, include_id=True,
            )
        for job_id in target_set & set(current_by_id):
            definition = definitions_by_id[job_id]
            if not _default_cron_behavior_contract(current_by_id[job_id]):
                raise RuntimeError("Legacy v3 rollback target behavior is not safely restorable")
            disabled = json.loads(json.dumps(_job_definition(definition)))
            disabled["enabled"] = False
            actual = _legacy_v3_job_contract_hash(
                current_by_id[job_id], include_id=True,
            )
            if actual not in {
                _legacy_v3_job_contract_hash(definition, include_id=True),
                _legacy_v3_job_contract_hash(disabled, include_id=True),
            }:
                raise RuntimeError("Legacy v3 rollback target drifted")
        for job_id in managed_set & set(current_by_id):
            job = current_by_id[job_id]
            key = job.get("declarationKey")
            if key == CRON_DECLARATION_KEY:
                exact = any(
                    _job_matches_spec(job, self._incremental_spec(), require_enabled=enabled)
                    for enabled in (True, False)
                )
            elif key == SNAPSHOT_CRON_DECLARATION_KEY:
                exact = any(
                    _job_matches_spec(job, self._snapshot_spec(), require_enabled=enabled)
                    for enabled in (True, False)
                )
            elif key == INITIAL_CRON_DECLARATION_KEY:
                exact = any(
                    self._initial_job_matches(job, enabled=enabled)
                    for enabled in (True, False)
                )
            else:
                exact = False
            if not exact:
                raise RuntimeError("Legacy v3 managed cron authority drifted")
        attributable = target_set | managed_set
        unexplained_managed = [
            job for job in current_jobs
            if job.get("declarationKey") in MANAGED_CRON_KEYS
            and str(job["id"]) not in attributable
        ]
        if unexplained_managed:
            raise RuntimeError(
                "Legacy v3 rollback found an unattributed managed cron; manual review required"
            )
        return attributable, upgraded_unknown_hashes

    def _nonce_rollback_removal_authority(
        self,
        transaction: dict[str, Any],
        current_jobs: list[dict[str, Any]],
        prior_definitions: list[dict[str, Any]],
    ) -> set[str]:
        """Bind nonce-era deletion authority to the complete durable receipt graph."""
        self._validate_replacement_receipt_graph(transaction)
        target_ids = transaction.get("cronTargetIdsBefore")
        managed_ids = transaction.get("managedCronIdsAfter")
        inventory_before = transaction.get("cronInventoryHashesBefore")
        unknown_before = transaction.get("cronUnknownHashesBefore")
        if transaction.get("cronContractHashVersion") != CRON_CONTRACT_HASH_VERSION \
                or not isinstance(target_ids, list) or not isinstance(managed_ids, list) \
                or any(
                    not isinstance(value, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(value)
                    for value in [*target_ids, *managed_ids]
                ) \
                or len(target_ids) != len(set(target_ids)) \
                or len(managed_ids) != len(set(managed_ids)) \
                or not isinstance(inventory_before, dict) \
                or not isinstance(unknown_before, dict) \
                or any(
                    not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    for receipt in (inventory_before, unknown_before)
                    for job_id, fingerprint in receipt.items()
                ):
            raise RuntimeError("Nonce rollback cron receipt graph is malformed")

        definitions_by_id: dict[str, dict[str, Any]] = {}
        for definition in prior_definitions:
            job_id = definition.get("id")
            if not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id) \
                    or job_id in definitions_by_id:
                raise RuntimeError("Nonce rollback definition identity is malformed")
            definitions_by_id[job_id] = definition
        target_set = set(target_ids)
        managed_set = set(managed_ids)
        if target_set != set(definitions_by_id) \
                or set(inventory_before) != target_set | set(unknown_before) \
                or target_set & set(unknown_before) \
                or managed_set & set(inventory_before):
            raise RuntimeError("Nonce rollback cron receipt graph is inconsistent")
        for job_id, definition in definitions_by_id.items():
            if inventory_before.get(job_id) != _job_contract_hash(
                definition, include_id=True,
            ):
                raise RuntimeError("Nonce rollback definition fingerprint drifted")

        staging_intents = self._validated_cron_intent_receipts(
            transaction, "cronStagingIntents",
        )
        checkpointed_ids = {
            intent["jobId"] for intent in staging_intents.values()
            if isinstance(intent.get("jobId"), str)
        }
        if managed_set != checkpointed_ids \
                or len(checkpointed_ids) != len([
                    intent for intent in staging_intents.values()
                    if isinstance(intent.get("jobId"), str)
                ]) \
                or checkpointed_ids & set(inventory_before):
            raise RuntimeError("Nonce rollback staging id authority is inconsistent")
        for intent in staging_intents.values():
            self._managed_intent_lifecycle_contracts(intent)

        definitions_by_key = {
            str(definition["declarationKey"]): definition
            for definition in prior_definitions
        }
        restore_intents = self._validated_cron_intent_receipts(
            transaction, "cronRestoreIntents",
        )
        if not set(restore_intents).issubset(definitions_by_key):
            raise RuntimeError("Nonce rollback restore intent is outside prior definitions")
        restored_receipt = transaction.get("restoredCronIdsByDeclaration", {})
        if not isinstance(restored_receipt, dict) or any(
            not isinstance(key, str) or key not in definitions_by_key
            or not isinstance(value, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(value)
            for key, value in restored_receipt.items()
        ):
            raise RuntimeError("Nonce rollback restored-id receipt is malformed")
        restore_ids: set[str] = set()
        for declaration_key, intent in restore_intents.items():
            definition = definitions_by_key[declaration_key]
            allowed_restore_fields = {
                "schema", "declarationKey", "role", "stagingName", "canonicalName",
                "canonicalDescription", "preAlertContractSha256", "jobId", "configured",
            }
            staging_name = intent.get("stagingName")
            if set(intent) - allowed_restore_fields \
                    or "configured" in intent and intent.get("configured") is not True \
                    or intent.get("canonicalName") != definition.get("name") \
                    or intent.get("canonicalDescription") != definition.get("description") \
                    or not isinstance(staging_name, str) \
                    or re.fullmatch(
                        r"qwen-restore-stage-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                        staging_name,
                    ) is None \
                    or intent.get("preAlertContractSha256") != _staging_contract_hash(
                        self._restore_pre_alert_definition(definition, staging_name)
                    ):
                raise RuntimeError("Nonce rollback restore intent semantic receipt drifted")
            job_id = intent.get("jobId")
            if isinstance(job_id, str):
                if job_id in set(inventory_before) | checkpointed_ids | restore_ids \
                        or restored_receipt.get(declaration_key) != job_id:
                    raise RuntimeError("Nonce rollback restore id authority is inconsistent")
                restore_ids.add(job_id)
            elif declaration_key in restored_receipt:
                raise RuntimeError("Nonce rollback restore id receipt lacks its intent id")
        if set(restored_receipt) != {
            key for key, intent in restore_intents.items()
            if isinstance(intent.get("jobId"), str)
        } or len(set(restored_receipt.values())) != len(restored_receipt):
            raise RuntimeError("Nonce rollback restored-id receipt is inconsistent")

        current_by_id = {str(job["id"]): job for job in current_jobs}
        for job_id, definition in definitions_by_id.items():
            current = current_by_id.get(job_id)
            if current is None:
                continue
            disabled = json.loads(json.dumps(definition))
            disabled["enabled"] = False
            actual = _job_contract_hash(current, include_id=True)
            if actual not in {
                inventory_before[job_id],
                _job_contract_hash(disabled, include_id=True),
            }:
                raise RuntimeError("Nonce rollback original cron identity was reused or drifted")

        removable_ids = target_set | checkpointed_ids
        for intent in staging_intents.values():
            job_id = intent.get("jobId")
            if isinstance(job_id, str):
                current = current_by_id.get(job_id)
                if current is not None and not self._managed_intent_lifecycle_matches(
                    current, intent,
                ):
                    raise RuntimeError(
                        "Nonce rollback checkpointed cron identity was reused or drifted"
                    )
                continue
            candidate = self._uncheckpointed_intent_candidate(intent, current_jobs)
            if candidate is not None:
                candidate_id = self._validate_intent_candidate_authority(
                    transaction, intent, candidate,
                )
                if not self._managed_intent_lifecycle_matches(candidate, intent):
                    raise RuntimeError("Nonce rollback uncheckpointed cron lifecycle drifted")
                removable_ids.add(candidate_id)

        for declaration_key, intent in restore_intents.items():
            definition = definitions_by_key[declaration_key]
            job_id = intent.get("jobId")
            if isinstance(job_id, str):
                current = current_by_id.get(job_id)
                if current is not None and not self._restore_lifecycle_matches(
                    current, definition, intent["stagingName"],
                ):
                    raise RuntimeError(
                        "Nonce rollback restored cron identity was reused or drifted"
                    )
                continue
            candidate = self._uncheckpointed_restore_intent_candidate(intent, current_jobs)
            if candidate is not None:
                candidate_id = self._validate_intent_candidate_authority(
                    transaction, intent, candidate,
                )
                if candidate_id in removable_ids or candidate_id in restore_ids:
                    raise RuntimeError("Nonce rollback restore candidate identity overlaps authority")
                restore_ids.add(candidate_id)

        for job_id, fingerprint in unknown_before.items():
            current = current_by_id.get(job_id)
            if current is None or _job_contract_hash(current, include_id=True) != fingerprint:
                raise RuntimeError("Nonce rollback unknown cron receipt drifted")

        allowed_ids = set(unknown_before) | target_set | removable_ids | restore_ids
        if set(current_by_id) - allowed_ids:
            raise RuntimeError("Nonce rollback inventory contains an unattributed cron job")
        baseline = self._inventory_hashes(current_jobs)
        if self._inventory_hashes(self._inventory()) != baseline:
            raise RuntimeError("Nonce rollback inventory changed after authority validation")
        return removable_ids

    def _remove_cron_ids_with_snapshot_guard(
        self, current_jobs: list[dict[str, Any]], removable_ids: set[str],
    ) -> None:
        """Remove exact IDs only while every remaining contract matches the validated snapshot."""
        remaining = self._inventory_hashes(current_jobs)
        if self._inventory_hashes(self._inventory()) != remaining:
            raise RuntimeError("Cron removal snapshot changed before mutation")
        for job_id in sorted(removable_ids & set(remaining)):
            fresh_inventory = self._inventory()
            if self._inventory_hashes(fresh_inventory) != remaining:
                raise RuntimeError("Cron removal snapshot drifted before exact-id deletion")
            fresh_by_id = {str(job["id"]): job for job in fresh_inventory}
            target = fresh_by_id.get(job_id)
            if target is None or _job_contract_hash(
                target, include_id=True,
            ) != remaining[job_id]:
                raise RuntimeError("Cron removal target contract drifted before exact-id deletion")
            if target.get("enabled") is not False:
                raise RuntimeError("Cron removal target is not disabled before exact-id deletion")
            if self._runtime_job_active(target):
                raise RuntimeError("Cron removal target became active before exact-id deletion")
            self.cli.run(["cron", "rm", job_id])
            remaining.pop(job_id)
            if self._inventory_hashes(self._inventory()) != remaining:
                raise RuntimeError("Cron removal changed more than its exact authorized id")

    def _rollback_locked(self, *, require_exact_post_config: bool = True) -> dict[str, Any]:
        transaction = self.store.read()
        activation_fail_safe = transaction.get("activationFailSafeRequired")
        if activation_fail_safe is not None and type(activation_fail_safe) is not bool:
            raise RuntimeError("Activation fail-safe durable marker is malformed")
        if activation_fail_safe is True:
            self._disable_uncommitted_managed_jobs_for_activation_failure(transaction)
            transaction = self.store.read()
        replacement_graph = self._validate_replacement_receipt_graph(transaction)
        rollback_origin_phase: str | None = None
        if replacement_graph:
            if transaction.get("phase") in {"failed", "rollback_failed"}:
                rollback_origin_phase = transaction.get("failurePhase")
            elif transaction.get("phase") == "rolled_back":
                rollback_origin_phase = transaction.get("rollbackOriginPhase")
            else:
                rollback_origin_phase = transaction.get("phase")
        self._snapshot_root_from_transaction(transaction)
        project_root = self._project_root_from_transaction(transaction)
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
        prepared_assets = self._preflight_rollback_assets(
            transaction, snapshot_path.parent,
        )
        self._preflight_created_snapshot_artifacts(transaction)
        if require_exact_post_config and transaction.get("postConfigSha256") and \
                self._sha256_config(config) != transaction["postConfigSha256"]:
            raise RuntimeError("OpenClaw config drifted after integration; refusing automatic rollback")
        prior_cron_definitions: list[dict[str, Any]] = []
        restored_cron_ids: list[str] = []
        if type(transaction.get("cronMutationStarted")) is not bool \
                or type(transaction.get("runtimeMutationStarted")) is not bool:
            raise RuntimeError("Rollback mutation markers are missing or malformed")
        cron_mutation_started = transaction["cronMutationStarted"]
        runtime_mutation_started = transaction["runtimeMutationStarted"]
        if runtime_mutation_started and not cron_mutation_started:
            raise RuntimeError("Rollback mutation marker ordering is inconsistent")
        cron_jobs_before_rollback: list[dict[str, Any]] | None = None
        cron_removable_ids: set[str] = set()
        unknown_hashes_before = transaction.get("cronUnknownHashesBefore", {})
        if not isinstance(unknown_hashes_before, dict) or any(
            not isinstance(key, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(key)
            or not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for key, value in unknown_hashes_before.items()
        ):
            raise RuntimeError("Rollback unknown-cron receipt is malformed")
        if transaction.get("contractVersion") == INTEGRATION_CONTRACT_VERSION:
            raw_definitions = transaction.get("cronDefinitionsBefore", [])
            if not isinstance(raw_definitions, list) or any(not isinstance(item, dict) for item in raw_definitions):
                raise RuntimeError("Rollback cron receipt is malformed")
            prior_cron_definitions = [_job_definition(item) for item in raw_definitions]
            restore_keys = [definition.get("declarationKey") for definition in prior_cron_definitions]
            if any(not isinstance(key, str) or not key for key in restore_keys) \
                    or len(restore_keys) != len(set(restore_keys)):
                raise RuntimeError("Rollback cron declaration receipt is malformed")
            existing_restore_intents = self._validated_cron_intent_receipts(
                transaction, "cronRestoreIntents",
            )
            if not set(existing_restore_intents).issubset(set(restore_keys)):
                raise RuntimeError("Rollback restore intent is outside durable definitions")
        if cron_mutation_started \
                and transaction.get("contractVersion") == INTEGRATION_CONTRACT_VERSION:
            cron_jobs_before_rollback = self._inventory()
            if "cronStagingIntents" in transaction:
                cron_removable_ids = self._nonce_rollback_removal_authority(
                    transaction, cron_jobs_before_rollback, prior_cron_definitions,
                )
            else:
                cron_removable_ids, unknown_hashes_before = (
                    self._legacy_v3_removable_cron_ids(
                        transaction, cron_jobs_before_rollback, prior_cron_definitions,
                    )
                )
        elif not cron_mutation_started:
            inventory_before = transaction.get("cronInventoryHashesBefore")
            if not isinstance(inventory_before, dict) or any(
                not isinstance(job_id, str) or not SAFE_CRON_JOB_ID_RE.fullmatch(job_id)
                or not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                for job_id, fingerprint in inventory_before.items()
            ):
                raise RuntimeError("Rollback non-mutation cron receipt is malformed")
            if self._inventory_hashes(self._inventory()) != inventory_before:
                raise RuntimeError("Cron inventory changed before rollback could start")

        if cron_mutation_started:
            if transaction.get("contractVersion") == INTEGRATION_CONTRACT_VERSION:
                if cron_jobs_before_rollback is None:
                    raise RuntimeError("Rollback cron authority was not prevalidated")
                cron_jobs_before_rollback = self._quiesce_rollback_removals(
                    cron_jobs_before_rollback, cron_removable_ids,
                )
        if runtime_mutation_started:
            precise_markers = any(
                field in transaction for field in (
                    "pluginMutationStarted", "configMutationStarted", "skillMutationStarted",
                    "projectRuntimeMutationStarted", "plistMutationStarted",
                    "launchdMutationStarted",
                )
            )
            legacy_runtime_receipt = not precise_markers
            plugin_mutation_started = transaction.get("pluginMutationStarted") is True
            config_mutation_started = transaction.get("configMutationStarted") is True \
                or legacy_runtime_receipt
            skill_mutation_started = transaction.get("skillMutationStarted") is True
            plist_mutation_started = transaction.get("plistMutationStarted") is True \
                or legacy_runtime_receipt
            launchd_mutation_started = transaction.get("launchdMutationStarted") is True \
                or legacy_runtime_receipt
            if launchd_mutation_started:
                self.deactivate_launchd()
            if plist_mutation_started:
                plist_backup = Path(transaction["plistBackupPath"])
                if transaction.get("plistExisted"):
                    self._restore_regular_file(plist_backup, self.paths.launchd_plist)
                else:
                    self.paths.launchd_plist.unlink(missing_ok=True)
            if plugin_mutation_started:
                spec, receipt = prepared_assets["plugin"]
                self._rollback_one_asset(
                    transaction, spec, receipt, snapshot_path.parent,
                )
            if skill_mutation_started:
                spec, receipt = prepared_assets["skill"]
                self._rollback_one_asset(
                    transaction, spec, receipt, snapshot_path.parent,
                )
            project_asset_ids = (
                "project.src", "project.scripts",
                "project.package_json", "project.package_lock",
            )
            if transaction.get("projectExisted"):
                for asset_id in project_asset_ids:
                    if asset_id not in prepared_assets:
                        continue
                    spec, receipt = prepared_assets[asset_id]
                    self._rollback_one_asset(
                        transaction, spec, receipt, snapshot_path.parent,
                    )
            elif transaction.get("projectCreated"):
                if not os.path.lexists(project_root):
                    pass
                else:
                    expected_dev = transaction.get("projectRootDev")
                    expected_ino = transaction.get("projectRootIno")
                    if type(expected_dev) is not int or type(expected_ino) is not int:
                        if self._legacy_unverified_created_root(
                            transaction,
                            prefix="projectRoot",
                            planned_key="projectCreatePlanned",
                            created_key="projectCreated",
                        ):
                            transaction["legacyUnverifiedProjectRootPreserved"] = True
                            self.store.write(transaction)
                            expected_dev = None
                            expected_ino = None
                        else:
                            raise RuntimeError("Created Qwen project identity is missing")
                    if expected_dev is None or expected_ino is None:
                        pass
                    else:
                        with self._open_private_directory(project_root.parent) as parent_fd:
                            self._quarantine_transaction_path(
                                transaction,
                                receipt_prefix="projectRoot",
                                parent_fd=parent_fd,
                                name=project_root.name,
                                expected_identity=(expected_dev, expected_ino),
                                directory=True,
                            )
            receipt_path = Path(transaction.get("healthReceiptPath", self.health_receipt_path))
            receipt_backup = Path(transaction.get(
                "healthReceiptBackupPath", Path(transaction["configBackupPath"]).parent / "health-receipt.preinstall.json"
            ))
            if transaction.get("healthReceiptExisted"):
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                self._restore_regular_file(receipt_backup, receipt_path)
                os.chmod(receipt_path, 0o600)
            elif receipt_path.exists() and not receipt_path.is_symlink():
                receipt_path.unlink()
            if config_mutation_started:
                self._restore_config_file(
                    snapshot_path, config,
                    expected_sha256=pre_config_sha256,
                    expected_run_identity=(run_dev, run_ino),
                    expected_marker_sha256=marker_sha256,
                )
                self.cli.run(["config", "validate", "--json"])
            if launchd_mutation_started and transaction.get("plistExisted"):
                self._bootstrap_launchd_plist(self.paths.launchd_plist)
            if config_mutation_started:
                self.cli.run(["gateway", "restart", "--safe", "--json"], timeout=300)
        if cron_mutation_started:
            if transaction.get("contractVersion") == INTEGRATION_CONTRACT_VERSION:
                if cron_jobs_before_rollback is None:
                    raise RuntimeError("Rollback cron authority was not prevalidated")
                self._remove_cron_ids_with_snapshot_guard(
                    cron_jobs_before_rollback, cron_removable_ids,
                )
            else:
                if transaction.get("cronId"):
                    self.cli.run(["cron", "rm", str(transaction["cronId"])], check=False)
                if transaction.get("initialIndexJobId"):
                    self.cli.run(
                        ["cron", "rm", str(transaction["initialIndexJobId"])], check=False,
                    )
            restored_cron_ids = [
                self._restore_cron_definition(item, transaction)
                for item in prior_cron_definitions
            ]
            self._verify_rollback_cron_state(
                prior_definitions=prior_cron_definitions,
                restored_ids=restored_cron_ids,
                unknown_hashes_before=unknown_hashes_before,
                force_disabled=True,
            )
            transaction["restoredCronIds"] = restored_cron_ids
            self.store.write(transaction)
        self._remove_created_snapshot_artifacts(transaction)
        if cron_mutation_started:
            self._activate_restored_cron_definitions(
                prior_definitions=prior_cron_definitions,
                restored_ids=restored_cron_ids,
                unknown_hashes_before=unknown_hashes_before,
            )
        transaction["restoredCronIds"] = restored_cron_ids
        quarantined = [
            resource for resource in (
                "indexLock", "snapshotLock", "projectRoot", "snapshotRoot",
                "projectParentCapabilityProbe", "snapshotParentCapabilityProbe",
            )
            if transaction.get(f"{resource}Quarantined") is True
            and transaction.get(f"{resource}QuarantinePurged") is not True
        ]
        preserved = [
            resource for resource, marker in (
                ("projectRoot", "legacyUnverifiedProjectRootPreserved"),
                ("snapshotRoot", "legacyUnverifiedSnapshotRootPreserved"),
                ("projectParentCapabilityProbe", "projectParentCapabilityProbePreserved"),
                ("snapshotParentCapabilityProbe", "snapshotParentCapabilityProbePreserved"),
            )
            if transaction.get(marker) is True
        ]
        transaction["rollbackQuarantinedResources"] = quarantined
        transaction["rollbackPreservedResources"] = preserved
        transaction["rollbackOutcome"] = (
            "restored_with_preserved_artifacts" if quarantined or preserved else "restored_exactly"
        )
        if transaction.get("activationFailSafeRequired") is True:
            transaction["activationFailSafeRequired"] = False
        if replacement_graph:
            transaction["rollbackOriginPhase"] = rollback_origin_phase
        transaction["phase"] = "rolled_back"
        self.store.write(transaction)
        return {
            "ok": True,
            "status": "ROLLED_BACK",
            "outcome": transaction["rollbackOutcome"],
        }

    def uninstall(self) -> dict[str, Any]:
        result = self.rollback(require_exact_post_config=True)
        result["preservedProject"] = True
        result["preservedRuntime"] = True
        return result
