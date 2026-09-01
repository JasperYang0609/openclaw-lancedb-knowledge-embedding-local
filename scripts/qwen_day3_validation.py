#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_SCRIPT = REPO_ROOT / "scripts" / "qwen_shadow_validation.py"
SANDBOX_PROFILE = (
    '(version 1)(allow default)'
    '(deny network-outbound (require-not (remote ip "localhost:*")))'
)
sys.path.insert(0, str(REPO_ROOT))

from scripts.qwen_shadow_validation import manager_for, managed_paths, resolve_specific  # noqa: E402


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    expect_success: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if expect_success and result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit {result.returncode}: {command[0]}\n"
            f"stdout tail: {result.stdout[-2000:]}\n"
            f"stderr tail: {result.stderr[-2000:]}"
        )
    return result


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def wait_for_exit(pid: int, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise RuntimeError(f"Process {pid} did not exit before timeout")


def wait_for_checkpoint(path: Path, minimum_rows: int, process: subprocess.Popen[str]) -> dict:
    deadline = time.time() + 180
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Shadow runner exited before interruption point: {process.returncode}")
        if path.is_file():
            checkpoint = json.loads(path.read_text())
            if int(checkpoint.get("completedRows") or 0) >= minimum_rows:
                return checkpoint
        time.sleep(0.25)
    raise RuntimeError("Shadow runner did not reach the forced-interruption checkpoint")


def row_snapshot(project: Path) -> dict:
    script = r"""
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import * as lancedb from '@lancedb/lancedb';
const config = JSON.parse(fs.readFileSync('config/source-map.json', 'utf8'));
const db = await lancedb.connect(path.resolve(config.dbPath));
const table = await db.openTable(config.tableName);
const rows = await table.query().select([
  'id', 'source_path', 'content_sha256', 'embedding_provider',
  'embedding_model', 'embedding_dimensions', 'vector'
]).toArray();
const stable = rows.map((row) => {
  const material = JSON.stringify({
    id: row.id,
    source_path: row.source_path,
    content_sha256: row.content_sha256,
    embedding_provider: row.embedding_provider,
    embedding_model: row.embedding_model,
    embedding_dimensions: Number(row.embedding_dimensions),
    vector: Array.from(row.vector)
  });
  return {
    id: row.id,
    source_path: row.source_path,
    content_sha256: row.content_sha256,
    digest: crypto.createHash('sha256').update(material).digest('hex')
  };
}).sort((a, b) => a.id.localeCompare(b.id));
console.log(JSON.stringify({ rows: stable.length, uniqueIds: new Set(stable.map((r) => r.id)).size, items: stable }));
"""
    result = run(["node", "--input-type=module", "-e", script], cwd=project)
    return json.loads(result.stdout)


def write_fixture_production(root: Path, count: int = 24) -> tuple[Path, Path]:
    production = root / "fixture-production"
    docs = production / "docs"
    (production / "config").mkdir(parents=True)
    docs.mkdir(parents=True)
    for index in range(count):
        (docs / f"fixture-{index:02d}.md").write_text(
            f"# Qwen Day 3 fixture {index}\n\n"
            f"This isolated record validates restart and resume behavior for item {index}.\n"
            f"The deterministic marker is DAY3-{index:02d}.\n"
        )
    config = {
        "version": 1,
        "dbPath": str((production / "data" / "lancedb").resolve()),
        "tableName": "production_fixture_unused",
        "privacy": {
            "discordRawApproval": "LOCAL_ONLY",
            "exactMessageIdValidation": "SKIPPED_PRIVACY_GATE",
        },
        "embedding": {"provider": "local-hash-v1", "model": "local-hash-v1", "dimensions": 64},
        "chunking": {"maxChars": 1000, "overlapChars": 0},
        "enrichment": {"enabled": False},
        "sources": [
            {
                "id": "day3-fixture",
                "project": "QwenDay3Fixture",
                "sourceType": "project_doc",
                "root": str(docs.resolve()),
                "include": ["**/*.md"],
                "exclude": [],
            }
        ],
    }
    atomic_json(production / "config" / "source-map.json", config)
    return production, docs


def prepare_validation(root: Path, production: Path, model: Path, server: Path, port: int) -> None:
    run(
        [
            sys.executable,
            str(VALIDATION_SCRIPT),
            "--root",
            str(root),
            "--port",
            str(port),
            "prepare",
            "--mode",
            "canary",
            "--production-project",
            str(production),
            "--model-source",
            str(model),
            "--server-source",
            str(server),
        ],
        timeout=600,
    )


def lifecycle_checks(root: Path, port: int, evidence: dict):
    manager = manager_for(root, port)

    first_pid = manager.start(timeout_seconds=180)
    assert manager.is_healthy() and manager.embedding_canary()
    manager.stop(timeout_seconds=30)
    wait_for_exit(first_pid)
    evidence["sigterm"] = {"pass": True, "pidExited": True, "pidFileRemoved": not manager.pid_file.exists()}

    second_pid = manager.start(timeout_seconds=180)
    assert second_pid != first_pid
    manager.stop(timeout_seconds=30)
    wait_for_exit(second_pid)
    evidence["restart"] = {"pass": True, "newPid": True, "singleManagedPid": True}

    atomic_json(manager.pid_file, {"schemaVersion": 1, "pid": 999_999_999, "port": port})
    stale_recovery_pid = manager.start(timeout_seconds=180)
    recorded = json.loads(manager.pid_file.read_text())
    assert recorded["pid"] == stale_recovery_pid
    manager.stop(timeout_seconds=30)
    wait_for_exit(stale_recovery_pid)
    evidence["stalePid"] = {"pass": True, "staleRecordReplaced": True}

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as collision:
        collision.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        collision.bind(("127.0.0.1", port))
        collision.listen(1)
        try:
            manager.start(timeout_seconds=5)
        except RuntimeError as error:
            assert "already in use" in str(error)
        else:
            raise AssertionError("Port collision did not fail closed")
    assert not manager.pid_file.exists()
    evidence["portCollision"] = {"pass": True, "failedClosed": True, "spawnedSidecar": False}

    killed_pid = manager.start(timeout_seconds=180)
    os.kill(killed_pid, signal.SIGKILL)
    assert manager.process is not None
    manager.process.wait(timeout=15)
    manager = manager_for(root, port)
    recovered_pid = manager.start(timeout_seconds=180)
    assert recovered_pid != killed_pid
    evidence["forcedSidecarInterruption"] = {"pass": True, "stalePidRecovered": True, "newPid": True}
    evidence["activePid"] = recovered_pid
    return manager


def runner_resume_checks(root: Path, port: int, evidence: dict) -> Path:
    paths = managed_paths(root, "canary")
    checkpoint_path = paths["shadow"] / "checkpoint.json"
    process = subprocess.Popen(
        ["node", "src/shadow-index.js", "--index-batch-size", "1"],
        cwd=paths["project"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    interrupted_checkpoint = wait_for_checkpoint(checkpoint_path, 3, process)
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)
    before_resume = row_snapshot(paths["project"])
    before_digests = {item["id"]: item["digest"] for item in before_resume["items"]}
    assert before_resume["rows"] >= int(interrupted_checkpoint["completedRows"])

    run(["node", "src/shadow-index.js", "--index-batch-size", "2"], cwd=paths["project"], timeout=600)
    after_resume = row_snapshot(paths["project"])
    after_digests = {item["id"]: item["digest"] for item in after_resume["items"]}
    assert all(after_digests[row_id] == digest for row_id, digest in before_digests.items())
    terminal = json.loads(checkpoint_path.read_text())
    assert terminal["status"] == "complete"
    assert terminal["completedRows"] == terminal["totalRows"] == after_resume["rows"]
    assert after_resume["rows"] == after_resume["uniqueIds"]

    run(["node", "src/shadow-index.js", "--index-batch-size", "2"], cwd=paths["project"], timeout=600)
    after_completed_rerun = row_snapshot(paths["project"])
    assert [item["digest"] for item in after_completed_rerun["items"]] == [
        item["digest"] for item in after_resume["items"]
    ]
    evidence["forcedRunnerInterruption"] = {
        "pass": True,
        "checkpointRowsBeforeKill": interrupted_checkpoint["completedRows"],
        "durableRowsObserved": before_resume["rows"],
    }
    evidence["checkpointResume"] = {
        "pass": True,
        "rows": after_resume["rows"],
        "uniqueIds": after_resume["uniqueIds"],
        "existingRowsRewritten": 0,
        "completedRerunRowsRewritten": 0,
    }
    return paths["project"]


def offline_query_check(project: Path, evidence: dict) -> None:
    remote = run(
        [
            "sandbox-exec",
            "-p",
            SANDBOX_PROFILE,
            "/usr/bin/curl",
            "-kfsSI",
            "--connect-timeout",
            "3",
            "https://1.1.1.1",
        ],
        expect_success=False,
        timeout=10,
    )
    assert remote.returncode != 0
    query = run(
        [
            "sandbox-exec",
            "-p",
            SANDBOX_PROFILE,
            "node",
            "src/cli.js",
            "search",
            "DAY3-07 restart resume",
            "--limit",
            "3",
        ],
        cwd=project,
        timeout=180,
    )
    assert "# Search:" in query.stdout and "## 1." in query.stdout
    evidence["offlineQuery"] = {
        "pass": True,
        "externalEgressDenied": True,
        "loopbackQuerySucceeded": True,
        "cloudFallback": False,
    }


def incremental_checks(project: Path, docs: Path, evidence: dict) -> None:
    run(["node", "src/cli.js", "index"], cwd=project, timeout=600)
    baseline = row_snapshot(project)
    baseline_by_path: dict[str, list[dict]] = {}
    for item in baseline["items"]:
        baseline_by_path.setdefault(item["source_path"], []).append(item)

    modified = docs / "fixture-00.md"
    deleted = docs / "fixture-01.md"
    added = docs / "fixture-added.md"
    modified.write_text(
        "# Qwen Day 3 fixture modified\n\n"
        "This record changed during the isolated incremental test. Marker DAY3-MODIFIED.\n"
    )
    deleted.unlink()
    added.write_text(
        "# Qwen Day 3 fixture added\n\n"
        "This record was added during the isolated incremental test. Marker DAY3-ADDED.\n"
    )

    first = run(["node", "src/cli.js", "incremental"], cwd=project, timeout=600)
    first_report = json.loads(first.stdout)
    after_first = row_snapshot(project)
    paths_after = {item["source_path"] for item in after_first["items"]}
    assert str(deleted.resolve()) not in paths_after
    assert str(modified.resolve()) in paths_after
    assert str(added.resolve()) in paths_after
    assert after_first["rows"] == baseline["rows"]
    assert after_first["uniqueIds"] == after_first["rows"]
    assert first_report["changedFiles"] == 2
    assert first_report["removedFiles"] == 1

    unchanged_paths = set(baseline_by_path) - {str(modified.resolve()), str(deleted.resolve())}
    after_first_by_path: dict[str, list[dict]] = {}
    for item in after_first["items"]:
        after_first_by_path.setdefault(item["source_path"], []).append(item)
    for source_path in unchanged_paths:
        assert [row["digest"] for row in baseline_by_path[source_path]] == [
            row["digest"] for row in after_first_by_path[source_path]
        ]

    second = run(["node", "src/cli.js", "incremental"], cwd=project, timeout=600)
    second_report = json.loads(second.stdout)
    after_second = row_snapshot(project)
    assert second_report["changedFiles"] == 0
    assert second_report["removedFiles"] == 0
    assert second_report["addedChunks"] == 0
    assert [item["digest"] for item in after_first["items"]] == [
        item["digest"] for item in after_second["items"]
    ]
    evidence["incrementalFixtures"] = {
        "pass": True,
        "added": 1,
        "modified": 1,
        "deleted": 1,
        "rowsAfter": after_second["rows"],
        "uniqueIds": after_second["uniqueIds"],
        "secondRunChangedFiles": second_report["changedFiles"],
        "secondRunAddedChunks": second_report["addedChunks"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the isolated Qwen Day 3 failure and resume validation")
    parser.add_argument("--root", required=True)
    parser.add_argument("--model-source", required=True)
    parser.add_argument("--server-source", required=True)
    parser.add_argument("--port", type=int, default=18889)
    args = parser.parse_args()

    root = resolve_specific(args.root)
    model = Path(args.model_source).expanduser().resolve()
    server = Path(args.server_source).expanduser().resolve()
    if root.exists():
        raise RuntimeError("Day 3 validation root must not already exist")
    if not model.is_file() or not server.is_file():
        raise FileNotFoundError("Verified model and runtime sources are required")
    if not 1024 <= args.port <= 65535:
        raise ValueError("Port must be from 1024 through 65535")

    root.mkdir(parents=True)
    evidence: dict = {
        "schemaVersion": 1,
        "status": "running",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "isolation": {
            "validationRootSpecific": True,
            "fixtureOnly": True,
            "productionGeminiTouched": False,
        },
        "artifacts": {
            "modelSha256": file_sha256(model),
            "runtimeSha256": file_sha256(server),
        },
    }
    manager = None
    try:
        production, docs = write_fixture_production(root)
        prepare_validation(root, production, model, server, args.port)
        manager = lifecycle_checks(root, args.port, evidence)
        project = runner_resume_checks(root, args.port, evidence)
        offline_query_check(project, evidence)
        incremental_checks(project, docs, evidence)
        evidence["status"] = "pass"
    except Exception as error:
        evidence["status"] = "fail"
        evidence["errorType"] = type(error).__name__
        evidence["error"] = str(error)[:1000]
        raise
    finally:
        if manager is None:
            try:
                manager = manager_for(root, args.port)
            except Exception:
                manager = None
        if manager is not None:
            try:
                manager.stop(timeout_seconds=30)
            except Exception as error:
                evidence["cleanupError"] = f"{type(error).__name__}: {str(error)[:500]}"
        evidence["completedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        evidence["isolation"]["productionGeminiTouched"] = False
        atomic_json(root / "reports" / "day3-validation.json", evidence)
        print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
