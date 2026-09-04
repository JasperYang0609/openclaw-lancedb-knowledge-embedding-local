#!/usr/bin/env python3
"""Write the bounded, redacted health receipt consumed by backup monitoring."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "backup-health-component.v1"
COMPONENT = "qwen-local"
ALLOWED_STATUS = {"ok", "warning", "error", "pending"}
DECLARATION_KEYS = {
    "incremental": "openclaw-lancedb-knowledge-local-incremental-v1",
    "initial": "openclaw-lancedb-knowledge-local-initial-v1",
    "snapshot": "openclaw-lancedb-knowledge-local-snapshot-v1",
}
FRESHNESS_MAX_AGE_SECONDS = 36 * 60 * 60
MAX_RECEIPT_BYTES = 16 * 1024
MAX_ITEMS = 20
MAX_INPUT_BYTES = 4 * 1024 * 1024
FORBIDDEN_TEXT = ("source_path", "query", "vector", "corpus", "token", "secret", "api_key")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_summary(value: str, *, limit: int = 240) -> str:
    text = " ".join(str(value).split())[:limit]
    if any(term in text.lower() for term in FORBIDDEN_TEXT):
        raise ValueError("Health receipt summary contains a forbidden detail")
    return text


def _stable(before: os.stat_result, after: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(before, field) == getattr(after, field) for field in fields)


@contextlib.contextmanager
def _open_parent(path: Path, *, private: bool = False, create: bool = False):
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute == Path("/"):
        raise RuntimeError("Health receipt path must be absolute and specific")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("Secure health receipt traversal is unsupported")
    descriptors: list[int] = []
    try:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for component in absolute.parent.parts[1:]:
            try:
                next_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=current)
                next_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current,
                )
            current = next_fd
            descriptors.append(current)
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH) \
                or private and metadata.st_mode & 0o077:
            raise RuntimeError("Health receipt parent ownership or permissions are unsafe")
        yield current
    except OSError as error:
        raise RuntimeError("Health receipt path is missing or unsafe") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_json(path: Path, *, max_bytes: int = MAX_INPUT_BYTES,
              private: bool = False, private_parent: bool = False) -> dict[str, Any]:
    if max_bytes < 1 or max_bytes > MAX_INPUT_BYTES:
        raise ValueError("Health receipt input size limit is invalid")
    descriptor: int | None = None
    with _open_parent(path, private=private_parent) as parent_fd:
        try:
            descriptor = os.open(
                Path(path).name,
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid() \
                    or before.st_mode & (0o077 if private else (stat.S_IWGRP | stat.S_IWOTH)) \
                    or before.st_size > max_bytes:
                raise RuntimeError("Health receipt input ownership, permissions, or size are unsafe")
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = None
                encoded = handle.read(max_bytes + 1)
                after = os.fstat(handle.fileno())
            if len(encoded) > max_bytes or not _stable(before, after):
                raise RuntimeError("Health receipt input changed or exceeded its bound")
        except OSError as error:
            raise RuntimeError("Health receipt input is missing or unsafe") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
    value = json.loads(encoded.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Health receipt input must be a JSON object")
    return value


def validate_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {
        "schema", "component", "producer", "declarationKey", "status", "checkedAt",
        "freshness", "summary", "checks", "metrics", "anomalies", "pending",
    }:
        raise ValueError("Health receipt top-level schema is invalid")
    if payload.get("schema") != SCHEMA or payload.get("component") != COMPONENT:
        raise ValueError("Health receipt identity is invalid")
    if payload.get("status") not in ALLOWED_STATUS:
        raise ValueError("Health receipt status is invalid")
    if payload.get("producer") != COMPONENT:
        raise ValueError("Health receipt producer is invalid")
    if payload.get("declarationKey") not in set(DECLARATION_KEYS.values()):
        raise ValueError("Health receipt declaration identity is invalid")
    if not isinstance(payload.get("checkedAt"), str):
        raise ValueError("Health receipt timestamp is invalid")
    try:
        checked_at = datetime.fromisoformat(payload["checkedAt"].replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Health receipt timestamp is invalid") from error
    if checked_at.tzinfo is None:
        raise ValueError("Health receipt timestamp must be timezone-aware")
    freshness = payload.get("freshness")
    if freshness != {"status": "current", "maxAgeSeconds": FRESHNESS_MAX_AGE_SECONDS}:
        raise ValueError("Health receipt freshness contract is invalid")
    payload["summary"] = _safe_summary(payload.get("summary", ""))
    for key in ("checks", "anomalies", "pending"):
        if not isinstance(payload.get(key), list) or len(payload[key]) > MAX_ITEMS:
            raise ValueError(f"Health receipt {key} is invalid")
    if not isinstance(payload.get("metrics"), dict):
        raise ValueError("Health receipt metrics are invalid")
    allowed_metrics = {"rows"}
    if set(payload["metrics"]) - allowed_metrics:
        raise ValueError("Health receipt contains an unsupported metric")
    if "rows" in payload["metrics"] and (
        type(payload["metrics"]["rows"]) is not int or payload["metrics"]["rows"] < 0
    ):
        raise ValueError("Health receipt row count is invalid")
    for check in payload["checks"]:
        if not isinstance(check, dict) or set(check) != {"key", "status", "summary"}:
            raise ValueError("Health receipt check schema is invalid")
        if check["status"] not in ALLOWED_STATUS:
            raise ValueError("Health receipt check status is invalid")
        check["key"] = _safe_summary(check["key"], limit=64)
        check["summary"] = _safe_summary(check["summary"])
    for anomaly in payload["anomalies"]:
        required = {"code", "summary", "impact", "dataLoss", "repairStatus"}
        if not isinstance(anomaly, dict) or set(anomaly) != required:
            raise ValueError("Health receipt anomaly schema is invalid")
        anomaly["code"] = _safe_summary(anomaly["code"], limit=64)
        for key in ("summary", "impact", "repairStatus"):
            anomaly[key] = _safe_summary(anomaly[key])
        if anomaly["dataLoss"] not in {"no", "yes", "unknown"}:
            raise ValueError("Health receipt data-loss state is invalid")
    payload["pending"] = [_safe_summary(item, limit=120) for item in payload["pending"]]
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ValueError("Health receipt exceeds the bounded size")
    return payload


def write_receipt(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise RuntimeError("Health receipt path is unsafe")
    payload = validate_receipt(payload)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ValueError("Health receipt exceeds the bounded size")
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    with _open_parent(path, create=True) as parent_fd:
        try:
            os.fchmod(parent_fd, 0o700)
            try:
                existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
                or existing.st_uid != os.getuid() or existing.st_mode & 0o077
            ):
                raise RuntimeError("Existing health receipt is unsafe")
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            readback_fd = os.open(
                path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                metadata = os.fstat(readback_fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 \
                        or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                    raise RuntimeError("Written health receipt is unsafe")
                readback = os.read(readback_fd, MAX_RECEIPT_BYTES + 1)
                after = os.fstat(readback_fd)
                if readback != encoded or not _stable(metadata, after):
                    raise RuntimeError("Written health receipt failed atomic readback")
            finally:
                os.close(readback_fd)
            os.fsync(parent_fd)
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise


OWNERSHIP_SCHEMA = "qwen-local-openclaw.v3"


def ownership_paths(manifest_path: Path) -> tuple[Path, Path]:
    if not manifest_path.is_absolute():
        raise RuntimeError("Qwen ownership manifest path is unsafe")
    manifest = load_json(manifest_path, private=True, private_parent=True)
    ownership = manifest.get("ownership")
    if manifest.get("schemaVersion") != 1 or manifest.get("phase") not in {"activation_pending", "committed"} \
            or not isinstance(ownership, dict) or ownership.get("schema") != OWNERSHIP_SCHEMA \
            or ownership.get("provider") != COMPONENT or ownership.get("localOnly") is not True \
            or ownership.get("healthReceiptSchema") != SCHEMA \
            or ownership.get("incrementalDeclarationKey") != DECLARATION_KEYS["incremental"] \
            or ownership.get("snapshotDeclarationKey") != DECLARATION_KEYS["snapshot"] \
            or ownership.get("initialDeclarationKey") != DECLARATION_KEYS["initial"]:
        raise RuntimeError("Qwen ownership manifest is missing or unsupported")
    project = Path(str(ownership.get("projectRoot", "")))
    receipt = Path(str(ownership.get("healthReceiptPath", "")))
    if not project.is_absolute() or not receipt.is_absolute() or receipt.parent != project / "reports" \
            or project.is_symlink() or receipt.is_symlink() \
            or any(parent.is_symlink() for parent in (*project.parents, *receipt.parents)):
        raise RuntimeError("Qwen ownership receipt boundary is invalid")
    return project, receipt


def build_receipt(
    *, event: str, status: str, rows: int | None = None, anomaly_code: str | None = None
) -> dict[str, Any]:
    if status not in ALLOWED_STATUS:
        raise ValueError("Unsupported health event status")
    if event not in DECLARATION_KEYS:
        raise ValueError("Unsupported health event identity")
    checks: list[dict[str, str]] = []
    pending: list[str] = []
    anomalies: list[dict[str, Any]] = []
    if event in {"incremental", "initial"}:
        checks.append({"key": "index", "status": status, "summary": "本機知識索引已驗證" if status == "ok" else "本機知識索引待完成"})
    elif event == "snapshot":
        checks.extend([
            {"key": "index", "status": "ok", "summary": "本機知識索引已驗證"},
            {"key": "snapshot", "status": status, "summary": "快照、校驗與隔離還原已通過" if status == "ok" else "快照驗證未完成"},
        ])
    if status == "pending":
        pending.append("initial-index-build")
    if status == "error":
        anomalies.append({
            "code": anomaly_code or "QWEN_COMPONENT_FAILED",
            "summary": "Qwen 本機備份元件驗證失敗",
            "impact": "搜尋索引或快照健康狀態尚未確認",
            "dataLoss": "unknown",
            "repairStatus": "等待下一輪重試或人工檢查",
        })
    elif status == "warning" and anomaly_code:
        anomalies.append({
            "code": anomaly_code,
            "summary": "Qwen 快照本輪因另一個快照仍在執行而安全跳過",
            "impact": "本輪尚未產生新的已驗證快照",
            "dataLoss": "no",
            "repairStatus": "既有執行完成後由下一輪重新驗證",
        })
    summaries = {
        "ok": "Qwen 本機索引與快照健康",
        "warning": "Qwen 本機快照本輪尚未完成驗證",
        "error": "Qwen 本機元件驗證失敗",
        "pending": "Qwen 本機索引正在建立",
    }
    return {
        "schema": SCHEMA,
        "component": COMPONENT,
        "producer": COMPONENT,
        "declarationKey": DECLARATION_KEYS[event],
        "status": status,
        "checkedAt": utc_now(),
        "freshness": {"status": "current", "maxAgeSeconds": FRESHNESS_MAX_AGE_SECONDS},
        "summary": summaries[status],
        "checks": checks,
        "metrics": {"rows": rows} if rows is not None else {},
        "anomalies": anomalies,
        "pending": pending,
    }


def read_verified_rows(project: Path) -> int | None:
    try:
        state = load_json(project / "data/index-state.json")
        ready = load_json(project / "data/openclaw-ready.json")
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None
    rows = state.get("chunks")
    if ready.get("ready") is not True or ready.get("provider") != "qwen-local" or ready.get("chunks") != rows:
        return None
    return rows if type(rows) is int and rows >= 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a redacted Qwen backup health component receipt")
    parser.add_argument("--ownership-manifest", required=True)
    parser.add_argument("--event", choices=("incremental", "initial", "snapshot"), required=True)
    parser.add_argument("--status", choices=tuple(sorted(ALLOWED_STATUS)), required=True)
    parser.add_argument("--anomaly-code")
    args = parser.parse_args()
    manifest_path = Path(os.path.abspath(Path(args.ownership_manifest).expanduser()))
    project, receipt_path = ownership_paths(manifest_path)
    rows = read_verified_rows(project)
    write_receipt(
        receipt_path,
        build_receipt(event=args.event, status=args.status, rows=rows, anomaly_code=args.anomaly_code),
    )
    print(json.dumps({"ok": True, "schema": SCHEMA, "component": COMPONENT, "status": args.status}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
