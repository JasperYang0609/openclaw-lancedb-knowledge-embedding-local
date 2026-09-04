from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import fcntl
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
INTEGRATION_CONTRACT_VERSION = 2
OWNERSHIP_SCHEMA = "qwen-local-openclaw.v2"
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
)
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


class IntegrationRollbackIncomplete(RuntimeError):
    """Integration failed and its automatic rollback could not be verified complete."""

    def __init__(self, original_error: Exception, rollback_error: Exception) -> None:
        super().__init__(
            "Integration failed and automatic rollback did not complete; state is recoverable"
        )
        self.original_error = original_error
        self.rollback_error = rollback_error
        self.recovery_state = "automatic_rollback_incomplete"


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
        *extra, "--declaration-key", CRON_DECLARATION_KEY, "--no-deliver", "--json",
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
        "--declaration-key", SNAPSHOT_CRON_DECLARATION_KEY, "--no-deliver", "--json",
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

    def add_args(self, *, disabled: bool = True) -> list[str]:
        args = [
            "cron", "add", "--name", self.name, "--description", self.description,
            "--session", self.session_target, "--cron", self.schedule,
            "--tz", self.timezone, "--exact", "--command-argv",
            json.dumps(list(self.argv), separators=(",", ":")),
            "--command-cwd", self.cwd, "--timeout-seconds", str(self.timeout_seconds),
            "--no-output-timeout-seconds", str(self.no_output_timeout_seconds),
            "--output-max-bytes", str(self.output_max_bytes),
        ]
        for key, value in self.command_env:
            args.extend(["--command-env", f"{key}={value}"])
        args.extend(["--declaration-key", self.key, "--no-deliver"])
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
            "--clear-tools", "--no-deliver", "--disable",
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

    if argv and same_script(argv[0]):
        return same_script(argv[0])
    safe_shells = {"sh", "/bin/sh", "bash", "/bin/bash", "zsh", "/bin/zsh"}
    return (
        len(argv) == 3
        and argv[0] in safe_shells
        and argv[1] == "-lc"
        and same_script(argv[2])
    )


def _job_targets_exact_script(job: dict[str, Any], expected_script: Path) -> bool:
    return _argv_targets_exact_script(_job_argv(job), expected_script)


def _job_targets_snapshot_wrapper(job: dict[str, Any], expected_wrapper: Path) -> bool:
    argv = _job_argv(job)
    expected = expected_wrapper.resolve(strict=False)
    for raw in argv:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute() and candidate.resolve(strict=False) == expected:
            return True
    return False


def _cron_jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if payload.get("truncated") is True or payload.get("hasMore") is not False or payload.get("nextCursor"):
            raise RuntimeError("OpenClaw cron inventory is incomplete")
        jobs = payload.get("jobs")
        total = payload.get("total")
        if type(total) is not int:
            raise RuntimeError("OpenClaw cron inventory total is missing")
    else:
        jobs = payload
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise RuntimeError("OpenClaw cron list returned an unexpected schema")
    if isinstance(payload, dict) and len(jobs) != total:
        raise RuntimeError("OpenClaw cron inventory count is incomplete")
    ids: list[str] = []
    keys: list[str] = []
    for job in jobs:
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
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


def _job_matches_spec(job: dict[str, Any], spec: ManagedCronSpec, *, require_enabled: bool) -> bool:
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
    alert = job.get("failureAlert") if isinstance(job.get("failureAlert"), dict) else {}
    if require_enabled and job.get("enabled", True) is False:
        return False
    if not require_enabled and job.get("enabled", True) is not False:
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
        job.get("declarationKey") == spec.key
        and job.get("name") == spec.name
        and job.get("description") == spec.description
        and job.get("sessionTarget") == spec.session_target
        and job.get("sessionKey") is None
        and job.get("agentId") is None
        and job.get("deleteAfterRun") in (None, False)
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
        and delivery == {"mode": "none"}
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
    return {key: job.get(key) for key in fields}


def _job_contract_hash(job: dict[str, Any], *, include_id: bool = False) -> str:
    encoded = json.dumps(
        _cron_contract_payload(job, include_id=include_id),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job_definition(job: dict[str, Any]) -> dict[str, Any]:
    """Persist a bounded, restorable definition allowlist, never arbitrary cron payload data."""
    raw = _cron_contract_payload(job, include_id=True)
    payload = raw.get("payload")
    schedule = raw.get("schedule")
    delivery = raw.get("delivery")
    alert = raw.get("failureAlert")
    allowed_payload = {
        "kind", "argv", "cwd", "timeoutSeconds", "noOutputTimeoutSeconds", "outputMaxBytes",
        "env", "toolsAllow",
    }
    if not isinstance(payload, dict) or set(payload) - allowed_payload:
        raise RuntimeError("Owned cron definition payload is outside the safe rollback allowlist")
    if not isinstance(schedule, dict) or set(schedule) - {"kind", "expr", "tz", "staggerMs", "at", "everyMs"}:
        raise RuntimeError("Owned cron definition schedule is outside the safe rollback allowlist")
    if delivery is not None and (
        not isinstance(delivery, dict) or set(delivery) - {"mode", "channel", "to", "accountId"}
    ):
        raise RuntimeError("Owned cron definition delivery is outside the safe rollback allowlist")
    if alert is not None and (
        not isinstance(alert, dict)
        or set(alert) - {"after", "cooldownMs", "includeSkipped", "mode", "channel", "to", "accountId"}
    ):
        raise RuntimeError("Owned cron definition alert is outside the safe rollback allowlist")
    argv = payload.get("argv")
    env = payload.get("env", {})
    tools = payload.get("toolsAllow", [])
    if not isinstance(argv, list) or not 1 <= len(argv) <= 16 \
            or any(not isinstance(item, str) or not item or len(item) > 8192 for item in argv):
        raise RuntimeError("Owned cron definition argv is outside the safe rollback allowlist")
    if not isinstance(env, dict) or len(env) > 16 \
            or any(not isinstance(key, str) or not isinstance(value, str) or len(key) > 128
                   or len(value) > 4096 or _sensitive_key(key) or _contains_forbidden_key(value)
                   for key, value in env.items()):
        raise RuntimeError("Owned cron definition environment is outside the safe rollback allowlist")
    if not isinstance(tools, list) or len(tools) > 32 \
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:*+/-]{1,128}", item)
                   for item in tools):
        raise RuntimeError("Owned cron definition tools are outside the safe rollback allowlist")
    if _contains_forbidden_key(raw):
        raise RuntimeError("Owned cron definition contains sensitive material")
    # JSON round-trip produces an owned deep copy without preserving attacker-controlled subclasses.
    return json.loads(json.dumps({key: value for key, value in raw.items() if value is not None}, ensure_ascii=False))


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
    def _safe_tree_sha256(root: Path, *, label: str) -> str:
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError(f"{label} is missing or unsafe")
        digest = hashlib.sha256()
        root_meta = root.stat()
        if root_meta.st_uid != os.getuid() or root_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(f"{label} ownership or permissions are unsafe")
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if path.is_symlink():
                if metadata.st_uid != os.getuid():
                    raise RuntimeError(f"{label} contains an unsafe symbolic link")
                target = IntegrationManager._safe_plugin_symlink(root, path, label=label)
                digest.update(b"L\0")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(target.encode("utf-8"))
                digest.update(b"\0")
                continue
            if metadata.st_uid != os.getuid() or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError(f"{label} contains an unsafe entry")
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(b"D\0")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
                digest.update(b"\0")
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError(f"{label} contains a non-regular or multiply linked file")
            digest.update(b"F\0")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                before = os.fstat(handle.fileno())
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = os.fstat(handle.fileno())
            IntegrationManager._assert_stable_file(before, after)
            digest.update(b"\0")
        return digest.hexdigest()

    def _copy_safe_tree(self, source: Path, target: Path, *, label: str) -> str:
        expected = self._safe_tree_sha256(source, label=label)
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"{label} backup target already exists")
        shutil.copytree(source, target, symlinks=True)
        if self._safe_tree_sha256(target, label=f"{label} backup") != expected:
            raise RuntimeError(f"{label} backup verification failed")
        return expected

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
        plugin_sha256 = None
        if plugin_target.is_symlink():
            raise RuntimeError("Existing OpenClaw plugin is a symbolic link")
        if plugin_existed:
            plugin_sha256 = self._copy_safe_tree(
                plugin_target, plugin_backup, label="Existing OpenClaw plugin",
            )
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
        if health_receipt.is_symlink():
            raise RuntimeError("Existing Qwen health receipt is a symbolic link")
        if health_receipt_existed:
            if not health_receipt.is_file():
                raise RuntimeError("Existing Qwen health receipt is unsafe")
            shutil.copy2(health_receipt, health_receipt_backup)
            os.chmod(health_receipt_backup, 0o600)
        return {
            "pluginTargetPath": str(plugin_target), "pluginBackupPath": str(plugin_backup),
            "pluginExisted": plugin_existed, "pluginBackupSha256": plugin_sha256,
            "skillTargetPath": str(skill_target), "skillBackupPath": str(skill_backup),
            "skillExisted": skill_existed, "plistBackupPath": str(plist_backup), "plistExisted": plist_existed,
            "projectExisted": project_existed, "projectBackupPath": str(project_backup),
            "healthReceiptPath": str(health_receipt),
            "healthReceiptBackupPath": str(health_receipt_backup),
            "healthReceiptExisted": health_receipt_existed,
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
            self._checkpoint_mutation(transaction, "pluginMutationStarted")
            self._checkpoint_mutation(transaction, "configMutationStarted")
            self.cli.run(["plugins", "install", "--force", str(plugin_archive)], timeout=600)
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
        self._checkpoint_mutation(transaction, "skillMutationStarted")
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

    def _prepare_snapshot_root(self) -> bool:
        existed = self.snapshot_root.exists()
        if not existed:
            self.snapshot_root.mkdir(parents=True, mode=0o700)
        os.chmod(self.snapshot_root, 0o700)
        metadata = self.snapshot_root.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise RuntimeError("Snapshot root ownership or permissions are unsafe")
        return not existed

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
        return [
            job for job in jobs
            if job.get("declarationKey") == GEMINI_DECLARATION_KEY
            and _job_argv(job) == [str(expected)]
            and _job_targets_exact_script(job, expected)
        ]

    @staticmethod
    def _inventory_hashes(jobs: list[dict[str, Any]]) -> dict[str, str]:
        return {
            str(job["id"]): _job_contract_hash(job, include_id=True)
            for job in jobs
        }

    @staticmethod
    def _runtime_job_active(job: dict[str, Any]) -> bool:
        state = job.get("state") if isinstance(job.get("state"), dict) else {}
        return state.get("status") in {"running", "starting"} or job.get("status") in {"running", "starting"}

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
                self.cli.run(["cron", "edit", job_id, "--disable"])
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

    @contextmanager
    def _runtime_quiescence_guard(self, *, timeout_seconds: float = 1800,
                                  poll_seconds: float = 1.0) -> Iterator[dict[str, Any]]:
        """Block new index/snapshot work while installer-owned runtime files are replaced."""
        if not self.paths.project_root.exists():
            yield {"snapshotLockCreated": False}
            return
        project = self.paths.project_root
        data = project / "data"
        _assert_no_symlink_components(data)
        data.mkdir(parents=True, exist_ok=True)
        data_meta = data.stat()
        if not stat.S_ISDIR(data_meta.st_mode) or data_meta.st_uid != os.getuid() \
                or data_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("Qwen data directory is unsafe for runtime quiescence")
        index_lock = data / "index.lock"
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        index_identity: tuple[int, int] | None = None
        snapshot_fd: int | None = None
        snapshot_locked = False
        snapshot_lock_created = False
        snapshot_identity: tuple[int, int] | None = None
        lock_receipt: dict[str, Any] = {
            "indexLockCreated": False,
            "snapshotLockCreated": False,
            "persisted": False,
        }
        try:
            while index_identity is None:
                try:
                    os.mkdir(index_lock, 0o700)
                    metadata = index_lock.lstat()
                    index_identity = (metadata.st_dev, metadata.st_ino)
                    lock_receipt.update({
                        "indexLockCreated": True,
                        "indexLockDev": metadata.st_dev,
                        "indexLockIno": metadata.st_ino,
                    })
                except FileExistsError:
                    if index_lock.is_symlink() or not index_lock.is_dir():
                        raise RuntimeError("Qwen index lock path is unsafe")
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Qwen index run did not quiesce before the bounded deadline")
                    time.sleep(min(max(0.01, poll_seconds), max(0.01, deadline - time.monotonic())))
            snapshot_root = self.snapshot_root
            snapshot_lock = snapshot_root / ".snapshot-run.lock"
            snapshot_lock_created = not os.path.lexists(snapshot_lock)
            snapshot_fd = os.open(snapshot_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            snapshot_meta = os.fstat(snapshot_fd)
            if not stat.S_ISREG(snapshot_meta.st_mode) or snapshot_meta.st_uid != os.getuid() \
                    or snapshot_meta.st_nlink != 1 or snapshot_meta.st_mode & 0o077:
                raise RuntimeError("Qwen snapshot run lock is unsafe")
            snapshot_identity = (snapshot_meta.st_dev, snapshot_meta.st_ino)
            lock_receipt.update({
                "snapshotLockCreated": snapshot_lock_created,
                "snapshotLockDev": snapshot_meta.st_dev,
                "snapshotLockIno": snapshot_meta.st_ino,
            })
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
            if snapshot_fd is not None:
                try:
                    if snapshot_locked:
                        fcntl.flock(snapshot_fd, fcntl.LOCK_UN)
                finally:
                    os.close(snapshot_fd)
            if snapshot_lock_created and lock_receipt.get("persisted") is not True \
                    and snapshot_identity is not None:
                try:
                    metadata = snapshot_lock.lstat()
                    if (metadata.st_dev, metadata.st_ino) != snapshot_identity \
                            or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise RuntimeError("Qwen snapshot quiescence lock identity changed")
                    snapshot_lock.unlink()
                except FileNotFoundError:
                    pass
            if index_identity is not None:
                try:
                    metadata = index_lock.lstat()
                    if (metadata.st_dev, metadata.st_ino) != index_identity or not stat.S_ISDIR(metadata.st_mode):
                        raise RuntimeError("Qwen index quiescence lock identity changed")
                    os.rmdir(index_lock)
                except FileNotFoundError as error:
                    raise RuntimeError("Qwen index quiescence lock disappeared") from error

    @staticmethod
    def _job_id_from_add(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise RuntimeError("OpenClaw cron add returned an unexpected schema")
        nested = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        job_id = payload.get("id") or nested.get("id")
        if not isinstance(job_id, str) or not job_id:
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
        tools = payload.get("toolsAllow")
        payload_keys = {
            "kind", "argv", "cwd", "timeoutSeconds", "noOutputTimeoutSeconds", "outputMaxBytes",
        }
        if tools is not None:
            payload_keys.add("toolsAllow")
            if not isinstance(tools, list) or len(tools) > 32 \
                    or any(not isinstance(item, str)
                           or not re.fullmatch(r"[A-Za-z0-9_.:*+/-]{1,128}", item) for item in tools):
                return False
        if set(payload) != payload_keys or payload.get("kind") != "command" \
                or _job_argv(job) != [str(self.paths.project_root / "scripts/knowledge_index_incremental.sh")] \
                or payload.get("cwd") != str(self.paths.project_root) \
                or payload.get("outputMaxBytes") != 65536:
            return False
        if job.get("declarationKey") != CRON_DECLARATION_KEY \
                or job.get("name") != "Qwen local knowledge incremental index" \
                or job.get("description") is not None \
                or job.get("enabled", True) not in (True, False) \
                or job.get("sessionTarget") != "isolated" \
                or job.get("sessionKey") is not None or job.get("agentId") is not None \
                or job.get("deleteAfterRun") not in (None, False) \
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
            and job.get("delivery") == {"mode": "none"}
            and job.get("failureAlert") in (None, {})
        )
        return production_variant or repository_variant

    def _legacy_snapshot_job_matches(self, job: dict[str, Any], *, require_known_key: bool) -> bool:
        if require_known_key and job.get("declarationKey") != LEGACY_SNAPSHOT_DECLARATION_KEY:
            return False
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        expected_argv = [
            "sh", "-lc", _legacy_snapshot_shell_command(
                project_root=self.paths.project_root,
                snapshot_root=self.snapshot_root,
                timezone_name=self.timezone_name,
            ),
        ]
        return (
            job.get("name") == LEGACY_SNAPSHOT_NAME
            and job.get("description") == LEGACY_SNAPSHOT_DESCRIPTION
            and job.get("enabled", True) in (True, False)
            and job.get("sessionTarget") == "isolated"
            and job.get("sessionKey") is None
            and job.get("agentId") is None
            and job.get("deleteAfterRun") in (None, False)
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
            "deleteAfterRun": None,
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
        legacy = self._legacy_snapshot_candidates(jobs)
        return jobs, legacy

    def _apply_managed_spec(self, spec: ManagedCronSpec, *, enable: bool = False) -> str:
        job_id = self._job_id_from_add(self.cli.json(spec.add_args(disabled=True)))
        self.cli.run(spec.alert_args(job_id))
        disabled = self._job_by_key(self._inventory(), spec.key)
        if disabled is None or disabled.get("id") != job_id or not _job_matches_spec(
            disabled, spec, require_enabled=False
        ):
            raise RuntimeError(f"Managed cron {spec.key} failed disabled readback verification")
        if enable:
            self.cli.run(["cron", "edit", job_id, "--enable"])
            enabled = self._job_by_key(self._inventory(), spec.key)
            if enabled is None or enabled.get("id") != job_id or not _job_matches_spec(
                enabled, spec, require_enabled=True
            ):
                raise RuntimeError(f"Managed cron {spec.key} failed enabled readback verification")
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
        for job_id in job_ids:
            self.cli.run(["cron", "edit", job_id, "--enable"])
        self._verify_recurring_specs(enabled=True)

    def create_incremental_cron(self) -> str:
        """Backward-compatible entry point; current installs use exact reconciliation."""
        job_id = self._apply_managed_spec(self._incremental_spec(), enable=True)
        return job_id

    def disable_owned_gemini_jobs(self) -> list[dict[str, Any]]:
        jobs = self._inventory()
        disabled = []
        for job in self._owned_gemini_jobs_exact(jobs):
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
            "contractVersion": INTEGRATION_CONTRACT_VERSION,
            "ownedAssets": [], **snapshot,
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
            existing = self._job_by_key(self._inventory(), INITIAL_CRON_DECLARATION_KEY)
            if existing is not None:
                self.cli.run(["cron", "rm", str(existing["id"])])
            return "READY", None
        full_script = self.paths.project_root / "scripts/knowledge_index_full.sh"
        if full_script.is_symlink() or not full_script.is_file():
            raise RuntimeError("Initial index wrapper is missing or unsafe")
        argv = [str(full_script), str(self.ownership_manifest)]
        payload = self.cli.json([
            "cron", "add", "--name", "Qwen local knowledge initial full index",
            "--description", INITIAL_CRON_DESCRIPTION, "--session", "isolated", "--at", "+5m",
            "--command-argv", json.dumps(argv, separators=(",", ":")),
            "--command-cwd", str(self.paths.project_root), "--timeout-seconds", "86400",
            "--no-output-timeout-seconds", "1800", "--output-max-bytes", "65536",
            "--command-env", f"QWEN_OWNERSHIP_MANIFEST={self.ownership_manifest}",
            "--command-env", f"QWEN_PYTHON={self.python_path}",
            "--command-env", f"OPENCLAW_LANCEDB_ROOT={self.paths.project_root}",
            "--declaration-key", INITIAL_CRON_DECLARATION_KEY,
            "--delete-after-run", "--no-deliver", "--disabled", "--json",
        ])
        job_id = self._job_id_from_add(payload)
        alert_spec = self._incremental_spec()
        self.cli.run(alert_spec.alert_args(job_id))
        job = self._job_by_key(self._inventory(), INITIAL_CRON_DECLARATION_KEY)
        if job is None or job.get("id") != job_id or not self._initial_job_matches(job, enabled=False):
            raise RuntimeError("Initial index job failed disabled readback verification")
        return "INDEX_BUILDING", job_id

    def _initial_job_matches(self, job: dict[str, Any], *, enabled: bool) -> bool:
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
            job.get("declarationKey") == INITIAL_CRON_DECLARATION_KEY
            and job.get("name") == "Qwen local knowledge initial full index"
            and job.get("description") == INITIAL_CRON_DESCRIPTION
            and job.get("enabled", True) is enabled
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
            and delivery == {"mode": "none"}
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
        self.cli.run(["cron", "edit", job_id, "--enable"])
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

    def _restore_cron_definition(self, definition: dict[str, Any]) -> str:
        if _contains_forbidden_key(definition):
            raise RuntimeError("Refusing to restore an unsafe cron definition")
        name = definition.get("name")
        schedule = definition.get("schedule") if isinstance(definition.get("schedule"), dict) else {}
        payload = definition.get("payload") if isinstance(definition.get("payload"), dict) else {}
        argv = _job_argv(definition)
        if not isinstance(name, str) or not name or not argv:
            raise RuntimeError("Owned cron rollback definition is incomplete")
        args = ["cron", "add", "--name", name]
        description = definition.get("description")
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
        tools_allow = payload.get("toolsAllow")
        if isinstance(tools_allow, list) and all(isinstance(item, str) for item in tools_allow):
            args.extend(["--tools", ",".join(tools_allow)])
        declaration = definition.get("declarationKey")
        if isinstance(declaration, str) and declaration:
            args.extend(["--declaration-key", declaration])
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
        job_id = self._job_id_from_add(self.cli.json(args))
        alert = definition.get("failureAlert") if isinstance(definition.get("failureAlert"), dict) else None
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
            self.cli.run(edit)
        if definition.get("enabled", True) is True:
            self.cli.run(["cron", "edit", job_id, "--enable"])
        return job_id

    def _verify_rollback_cron_state(
        self,
        *,
        prior_definitions: list[dict[str, Any]],
        restored_ids: list[str],
        unknown_hashes_before: dict[str, str],
    ) -> None:
        if len(restored_ids) != len(prior_definitions) or len(set(restored_ids)) != len(restored_ids):
            raise RuntimeError("Rollback cron restoration set is incomplete")
        jobs = self._inventory()
        by_id = {str(job["id"]): job for job in jobs}
        for definition, restored_id in zip(prior_definitions, restored_ids):
            restored = by_id.get(restored_id)
            if restored is None or _job_contract_hash(restored) != _job_contract_hash(definition):
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

    def _verify_activation_pending(self, transaction: dict[str, Any]) -> None:
        if transaction.get("phase") != "activation_pending" or transaction.get("ownership") != self._ownership_payload():
            raise RuntimeError("Qwen activation transaction is not ready for final verification")
        jobs, legacy = self._preflight_cron_inventory()
        if legacy:
            raise RuntimeError("Legacy snapshot declaration remains during activation")
        self._verify_approved_collision_receipt(transaction, jobs)
        for spec in (self._incremental_spec(), self._snapshot_spec()):
            job = self._job_by_key(jobs, spec.key)
            if job is None or not _job_matches_spec(job, spec, require_enabled=True):
                raise RuntimeError("Recurring cron activation verification failed")
        if transaction.get("indexState") == "INDEX_BUILDING":
            initial = self._job_by_key(jobs, INITIAL_CRON_DECLARATION_KEY)
            if initial is None or not self._initial_job_matches(initial, enabled=True):
                raise RuntimeError("Initial index activation verification failed")
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
        initial = self._job_by_key(jobs, INITIAL_CRON_DECLARATION_KEY)
        if manifest.get("indexState") == "INDEX_BUILDING" and (
            initial is None or not self._initial_job_matches(initial, enabled=True)
        ):
            raise RuntimeError("Pending initial index job does not match the exact managed contract")
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
            "indexState": manifest.get("indexState"),
            "healthReceiptStatus": self._health_receipt_status(),
        }

    def integrate(self, runtime_manifest: dict[str, Any]) -> dict[str, Any]:
        with self._integration_lock():
            return self._integrate_locked(runtime_manifest)

    def _integrate_locked(self, runtime_manifest: dict[str, Any]) -> dict[str, Any]:
        prior: dict[str, Any] | None = None
        if self.store.manifest_path.is_file() and not self.store.manifest_path.is_symlink():
            existing = self.store.read()
            if existing.get("phase") == "committed":
                prior = existing
                if existing.get("contractVersion") == INTEGRATION_CONTRACT_VERSION:
                    try:
                        return {"status": existing.get("indexState"), "transaction": "already_current", **self.verify()}
                    except Exception:
                        pass
            elif existing.get("phase") != "rolled_back":
                raise RuntimeError("An unfinished OpenClaw integration transaction requires rollback")
        jobs_before, legacy_before = self._preflight_cron_inventory()
        gemini_before = self._owned_gemini_jobs_exact(jobs_before)
        prior_definitions = [
            _job_definition(job) for job in jobs_before
            if job.get("declarationKey") in {
                CRON_DECLARATION_KEY, SNAPSHOT_CRON_DECLARATION_KEY, INITIAL_CRON_DECLARATION_KEY,
            } or any(job.get("id") == legacy.get("id") for legacy in legacy_before)
            or any(job.get("id") == gemini.get("id") for gemini in gemini_before)
        ]
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
        transaction = self.begin()
        try:
            transaction["previousPhase"] = prior.get("phase") if prior else None
            transaction["previousContractVersion"] = prior.get("contractVersion", 1) if prior else None
            transaction["cronDefinitionsBefore"] = prior_definitions
            transaction["cronInventoryTotalBefore"] = len(jobs_before)
            transaction["cronInventoryHashesBefore"] = inventory_hashes_before
            transaction["cronUnknownHashesBefore"] = unknown_hashes_before
            transaction["cronTargetIdsBefore"] = sorted(target_ids)
            transaction["cronMutationStarted"] = False
            transaction["runtimeMutationStarted"] = False
            transaction["pluginMutationStarted"] = False
            transaction["configMutationStarted"] = False
            transaction["skillMutationStarted"] = False
            transaction["plistMutationStarted"] = False
            transaction["launchdMutationStarted"] = False
            transaction["disabledGeminiJobs"] = planned_gemini_disables
            transaction["ownership"] = self._ownership_payload()
            transaction["runtimePort"] = int(runtime_manifest["runtimePort"])
            transaction["snapshotRootCreated"] = not self.snapshot_root.exists()
            transaction["projectCreated"] = not transaction.get("projectExisted", False)
            transaction["phase"] = "preflight_complete"
            self.store.write(transaction)
            transaction["cronMutationStarted"] = True
            transaction["phase"] = "quiescing"
            self.store.write(transaction)
            transaction["quiescedCronIds"] = self._quiesce_prior_jobs(
                jobs_before, target_ids, inventory_hashes_before,
            )
            created_snapshot_root = self._prepare_snapshot_root()
            if created_snapshot_root != transaction["snapshotRootCreated"]:
                raise RuntimeError("Snapshot root identity changed during integration preflight")
            transaction["phase"] = "quiesced"
            self.store.write(transaction)

            with self._runtime_quiescence_guard() as lock_receipt:
                transaction.update({
                    key: value for key, value in lock_receipt.items() if key != "persisted"
                })
                self.store.write(transaction)
                lock_receipt["persisted"] = True
                transaction["runtimeMutationStarted"] = True
                transaction["phase"] = "staging"
                self.store.write(transaction)
                actual_project_created = self.bootstrap_project(runtime_manifest)
                if actual_project_created != transaction["projectCreated"] and not transaction.get("projectExisted"):
                    raise RuntimeError("Qwen project creation state changed during integration")
                transaction["projectCreated"] = actual_project_created
                if not actual_project_created:
                    self.synchronize_project_runtime()
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
                transaction["cronId"] = self._apply_managed_spec(self._incremental_spec())
                transaction["snapshotCronId"] = self._apply_managed_spec(self._snapshot_spec())
                transaction["managedCronIdsAfter"] = [transaction["cronId"], transaction["snapshotCronId"]]
                self._verify_recurring_specs(enabled=False)
                self.store.write(transaction)
                for legacy in legacy_before:
                    self.cli.run(["cron", "rm", str(legacy["id"])])
                if legacy_before:
                    current_ids = {str(job["id"]) for job in self._inventory()}
                    if any(str(legacy["id"]) in current_ids for legacy in legacy_before):
                        raise RuntimeError("Legacy snapshot declaration remained after explicit migration")
                    transaction["removedLegacySnapshotJobs"] = [str(job["id"]) for job in legacy_before]
                current_by_id = {str(job["id"]): job for job in self._inventory()}
                if any(current_by_id.get(item["id"], {}).get("enabled", True) is not False
                       for item in planned_gemini_disables):
                    raise RuntimeError("Gemini rollback declarations did not remain quiesced")
                transaction["indexState"], transaction["initialIndexJobId"] = self.mark_ready_or_schedule_build()
                if transaction["initialIndexJobId"]:
                    transaction["managedCronIdsAfter"].append(transaction["initialIndexJobId"])
                self.cli.run(["config", "validate", "--json"])
                config = Path(transaction["configPath"])
                transaction["postConfigSha256"] = self._sha256_config(config)
                transaction["phase"] = "restarting_gateway"
                self.store.write(transaction)
                self.cli.run(["gateway", "restart", "--safe", "--json"], timeout=300)
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
            transaction["phase"] = "committed"
            self.store.write(transaction)
            verification = self.verify()
            action = "upgraded" if prior else "committed"
            return {"status": transaction["indexState"], "transaction": action, **verification}
        except Exception as original_error:
            try:
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
                    transaction["phase"] = "rollback_failed"
                    self.store.write(transaction)
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

    def _remove_created_snapshot_artifacts(self, transaction: dict[str, Any]) -> None:
        if transaction.get("indexLockCreated") is True:
            expected_dev = transaction.get("indexLockDev")
            expected_ino = transaction.get("indexLockIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                raise RuntimeError("Created index lock identity is missing")
            data = self.paths.project_root / "data"
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
                    os.rmdir("index.lock", dir_fd=data_fd)
                    os.fsync(data_fd)
                    try:
                        os.stat("index.lock", dir_fd=data_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise RuntimeError("Created index lock remained after rollback")
            except OSError as error:
                raise RuntimeError("Created index lock could not be safely removed") from error
            finally:
                if data_fd is not None:
                    os.close(data_fd)

        root = Path(os.path.abspath(self.snapshot_root))
        if transaction.get("snapshotLockCreated") is True:
            expected_dev = transaction.get("snapshotLockDev")
            expected_ino = transaction.get("snapshotLockIno")
            if type(expected_dev) is not int or type(expected_ino) is not int:
                raise RuntimeError("Created snapshot lock identity is missing")
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
                    os.unlink(".snapshot-run.lock", dir_fd=root_fd)
                    os.fsync(root_fd)
                    try:
                        os.stat(".snapshot-run.lock", dir_fd=root_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise RuntimeError("Created snapshot lock remained after rollback")
            except OSError as error:
                raise RuntimeError("Created snapshot lock could not be safely removed") from error
            finally:
                if root_fd is not None:
                    os.close(root_fd)
        if transaction.get("snapshotRootCreated") is True:
            if root.is_symlink() or not root.is_dir():
                raise RuntimeError("Created snapshot root changed before rollback")
            metadata = root.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                    or metadata.st_mode & 0o077:
                raise RuntimeError("Created snapshot root is unsafe during rollback")
            try:
                root.rmdir()
            except OSError as error:
                raise RuntimeError("Created snapshot root is not empty during rollback") from error
            if os.path.lexists(root):
                raise RuntimeError("Created snapshot root remained after rollback")

    def _restore_plugin_from_snapshot(
        self, transaction: dict[str, Any], *, snapshot_path: Path,
    ) -> None:
        target_value = transaction.get("pluginTargetPath")
        backup_value = transaction.get("pluginBackupPath")
        if not isinstance(target_value, str) or Path(target_value) != self.plugin_target:
            raise RuntimeError("Rollback plugin target identity is missing or unsafe")
        expected_backup = snapshot_path.parent / "plugin.preinstall"
        if not isinstance(backup_value, str) or Path(backup_value) != expected_backup:
            raise RuntimeError("Rollback plugin backup identity is missing or unsafe")
        plugin_existed = transaction.get("pluginExisted") is True
        expected_sha256 = transaction.get("pluginBackupSha256")
        if plugin_existed:
            if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
                raise RuntimeError("Rollback plugin backup checksum is missing")
            if self._safe_tree_sha256(expected_backup, label="Rollback plugin backup") != expected_sha256:
                raise RuntimeError("Rollback plugin backup checksum mismatch")
        elif expected_sha256 is not None:
            raise RuntimeError("Rollback plugin backup receipt is inconsistent")
        if self.plugin_target.is_symlink() or (
            self.plugin_target.exists() and not self.plugin_target.is_dir()
        ):
            raise RuntimeError("Installed plugin became unsafe before rollback")
        self.cli.run(["plugins", "uninstall", PLUGIN_ID, "--force"], check=False)
        if self.plugin_target.exists():
            shutil.rmtree(self.plugin_target)
        if plugin_existed:
            restored_sha256 = self._copy_safe_tree(
                expected_backup, self.plugin_target, label="Rollback OpenClaw plugin",
            )
            if restored_sha256 != expected_sha256:
                raise RuntimeError("Rollback plugin restoration did not match its snapshot")

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
        prior_cron_definitions: list[dict[str, Any]] = []
        restored_cron_ids: list[str] = []
        cron_mutation_started = transaction.get("cronMutationStarted") is True
        runtime_mutation_started = transaction.get("runtimeMutationStarted") is True
        unknown_hashes_before = transaction.get("cronUnknownHashesBefore", {})
        if not isinstance(unknown_hashes_before, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or len(value) != 64
            for key, value in unknown_hashes_before.items()
        ):
            raise RuntimeError("Rollback unknown-cron receipt is malformed")
        if transaction.get("contractVersion") == INTEGRATION_CONTRACT_VERSION:
            raw_definitions = transaction.get("cronDefinitionsBefore", [])
            if not isinstance(raw_definitions, list) or any(not isinstance(item, dict) for item in raw_definitions):
                raise RuntimeError("Rollback cron receipt is malformed")
            prior_cron_definitions = raw_definitions
        if runtime_mutation_started:
            precise_markers = any(
                field in transaction for field in (
                    "pluginMutationStarted", "configMutationStarted", "skillMutationStarted",
                    "plistMutationStarted", "launchdMutationStarted",
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
                self._restore_plugin_from_snapshot(transaction, snapshot_path=snapshot_path)
            if skill_mutation_started:
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
                current_jobs = self._inventory()
                target_ids_before = transaction.get("cronTargetIdsBefore", [])
                managed_ids_after = transaction.get("managedCronIdsAfter", [])
                if not isinstance(target_ids_before, list) or not isinstance(managed_ids_after, list):
                    raise RuntimeError("Rollback cron target receipt is malformed")
                removable_ids = {
                    *(str(value) for value in target_ids_before),
                    *(str(value) for value in managed_ids_after),
                }
                for job in current_jobs:
                    job_id = str(job["id"])
                    if job.get("declarationKey") in MANAGED_CRON_KEYS | {LEGACY_SNAPSHOT_DECLARATION_KEY} \
                            or job_id in removable_ids:
                        self.cli.run(["cron", "rm", job_id])
            else:
                if transaction.get("cronId"):
                    self.cli.run(["cron", "rm", str(transaction["cronId"])], check=False)
                if transaction.get("initialIndexJobId"):
                    self.cli.run(["cron", "rm", str(transaction["initialIndexJobId"])], check=False)
            restored_cron_ids = [self._restore_cron_definition(item) for item in prior_cron_definitions]
            self._verify_rollback_cron_state(
                prior_definitions=prior_cron_definitions,
                restored_ids=restored_cron_ids,
                unknown_hashes_before=unknown_hashes_before,
            )
        elif transaction.get("cronInventoryHashesBefore"):
            if self._inventory_hashes(self._inventory()) != transaction["cronInventoryHashesBefore"]:
                raise RuntimeError("Cron inventory changed before rollback could start")
        transaction["restoredCronIds"] = restored_cron_ids
        self._remove_created_snapshot_artifacts(transaction)
        transaction["phase"] = "rolled_back"
        self.store.write(transaction)
        return {"ok": True, "status": "ROLLED_BACK"}

    def uninstall(self) -> dict[str, Any]:
        result = self.rollback(require_exact_post_config=True)
        result["preservedProject"] = True
        result["preservedRuntime"] = True
        return result
