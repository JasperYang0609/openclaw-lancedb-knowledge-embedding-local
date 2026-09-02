from __future__ import annotations

import json
import os
import plistlib
import sys
from pathlib import Path

import pytest

import src.openclaw_integration.core as integration_core

from src.openclaw_integration.core import (
    CRON_DECLARATION_KEY,
    PLUGIN_ID,
    IntegrationPaths,
    TransactionStore,
    build_cron_add_args,
    merge_allowlist,
    owned_gemini_jobs,
    IntegrationManager,
)
from src.openclaw_integration.launchd import build_launchd_plist


def paths(tmp_path: Path) -> IntegrationPaths:
    home = tmp_path / "home"
    workspace = home / ".openclaw/workspace"
    project = workspace / "knowledge-lancedb-qwen-local"
    runtime = home / "Library/Application Support/OpenClaw/qwen-local"
    state = home / "Library/Application Support/OpenClaw/qwen-local-integration"
    plist = home / "Library/LaunchAgents/ai.openclaw.qwen-local-embedding.plist"
    for directory in (workspace, project, runtime, state, plist.parent):
        directory.mkdir(parents=True, exist_ok=True)
    return IntegrationPaths(home=home, workspace=workspace, project_root=project, runtime_root=runtime,
                            state_root=state, launchd_plist=plist)


def test_paths_reject_wrong_project_identity_and_symlink(tmp_path: Path) -> None:
    item = paths(tmp_path)
    item.validate()
    with pytest.raises(ValueError, match="Qwen project"):
        IntegrationPaths(home=item.home, workspace=item.workspace, project_root=item.workspace / "knowledge-lancedb",
                         runtime_root=item.runtime_root, state_root=item.state_root,
                         launchd_plist=item.launchd_plist).validate()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = item.workspace / "knowledge-lancedb-qwen-local"
    linked.rmdir()
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        IntegrationPaths(home=item.home, workspace=item.workspace, project_root=linked,
                         runtime_root=item.runtime_root, state_root=item.state_root,
                         launchd_plist=item.launchd_plist).validate()


def test_transaction_store_is_restricted_atomic_and_rejects_secrets(tmp_path: Path) -> None:
    item = paths(tmp_path)
    store = TransactionStore(item.state_root)
    manifest = store.write({"schemaVersion": 1, "runId": "run-1", "phase": "prepared", "ownedAssets": []})
    assert manifest.stat().st_mode & 0o077 == 0
    assert store.read()["runId"] == "run-1"
    with pytest.raises(ValueError, match="forbidden"):
        store.write({"schemaVersion": 1, "runId": "run-2", "token": "do-not-store"})


def test_launchd_plist_uses_fixed_argv_loopback_and_no_shell(tmp_path: Path) -> None:
    item = paths(tmp_path)
    server = item.runtime_root / "runtime/llama-server"
    model = item.runtime_root / "models/model.gguf"
    key = item.runtime_root / "run/api-key"
    for file_path in (server, model, key):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("fixture")
    payload = build_launchd_plist(server=server, model=model, api_key_file=key, port=18889,
                                  stdout_path=item.state_root / "logs/server.out.log",
                                  stderr_path=item.state_root / "logs/server.err.log")
    parsed = plistlib.loads(payload)
    argv = parsed["ProgramArguments"]
    assert argv[0] == str(server)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "18889"
    assert "--api-key-file" in argv
    assert "/bin/sh" not in argv and "bash" not in argv
    assert parsed["RunAtLoad"] is True


def test_cron_contract_is_idempotent_fixed_argv_and_output_bounded(tmp_path: Path) -> None:
    item = paths(tmp_path)
    node = tmp_path / "node"
    script = item.project_root / "scripts/knowledge_index_incremental.sh"
    node.write_text("fixture")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("fixture")
    args = build_cron_add_args(project_root=item.project_root, incremental_script=script)
    assert args[args.index("--declaration-key") + 1] == CRON_DECLARATION_KEY
    assert json.loads(args[args.index("--command-argv") + 1]) == [str(script)]
    assert args[args.index("--command-cwd") + 1] == str(item.project_root)
    assert int(args[args.index("--output-max-bytes") + 1]) <= 65536
    assert "--command" not in args


def test_allowlists_merge_without_removing_existing_entries() -> None:
    assert merge_allowlist(["message", "status_update_ui"], PLUGIN_ID) == [
        "message", "status_update_ui", PLUGIN_ID,
    ]
    assert merge_allowlist(None, PLUGIN_ID) is None
    assert merge_allowlist(None, PLUGIN_ID, create_if_missing=True) == [PLUGIN_ID]


def test_only_exact_owned_gemini_jobs_are_selected() -> None:
    jobs = [
        {"id": "owned", "declarationKey": "openclaw-lancedb-knowledge-gemini-incremental-v1",
         "payload": {"command": {"argv": ["/safe/knowledge-lancedb/scripts/knowledge_index_incremental.sh"]}}},
        {"id": "name-only", "name": "Gemini knowledge incremental"},
        {"id": "wrong-command", "declarationKey": "openclaw-lancedb-knowledge-gemini-incremental-v1",
         "payload": {"command": {"argv": ["/tmp/attacker.sh"]}}},
    ]
    assert [job["id"] for job in owned_gemini_jobs(jobs)] == ["owned"]


def test_runtime_sync_preserves_config_data_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = paths(tmp_path)
    for relative, content in (("config/source-map.json", "{\"keep\":true}"),
                              ("data/existing.index", "keep-index"), ("reports/existing.json", "keep-report"),
                              ("src/old.js", "old"), ("scripts/old.sh", "old"),
                              ("package.json", "{}"), ("package-lock.json", "{}")):
        target = item.project_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    fake_npm = tmp_path / "npm"
    fake_npm.write_text("fixture")
    fake_npm.chmod(0o700)
    calls = []
    monkeypatch.setattr(integration_core.shutil, "which", lambda name: str(fake_npm) if name == "npm" else None)
    monkeypatch.setattr(integration_core.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    manager = IntegrationManager(paths=item, repo_root=Path(__file__).resolve().parents[1], cli=None,
                                 node_path=Path(sys.executable))

    manager.synchronize_project_runtime()

    assert (item.project_root / "config/source-map.json").read_text() == "{\"keep\":true}"
    assert (item.project_root / "data/existing.index").read_text() == "keep-index"
    assert (item.project_root / "reports/existing.json").read_text() == "keep-report"
    assert not (item.project_root / "src/old.js").exists()
    assert (item.project_root / "src/cli.js").is_file()
    assert calls[0][0][-2:] == ["ci", "--ignore-scripts"]
    assert calls[0][1]["shell"] is False


def test_plugin_is_packed_as_archive_without_lifecycle_scripts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = paths(tmp_path)
    fake_npm = tmp_path / "npm"
    fake_npm.write_text("fixture")
    fake_npm.chmod(0o700)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        staging = Path(argv[argv.index("--pack-destination") + 1])
        archive = staging / "openclaw-lancedb-knowledge-local-plugin-0.1.0.tgz"
        archive.write_bytes(b"fixture")
        return integration_core.subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([{"filename": archive.name}]), stderr="",
        )

    monkeypatch.setattr(integration_core.shutil, "which", lambda name: str(fake_npm) if name == "npm" else None)
    monkeypatch.setattr(integration_core.subprocess, "run", fake_run)
    manager = IntegrationManager(paths=item, repo_root=Path(__file__).resolve().parents[1], cli=None,
                                 node_path=Path(sys.executable))

    archive = manager.package_plugin_archive()

    assert archive.is_file()
    assert calls[0][0][1:3] == ["pack", "--json"]
    assert "--ignore-scripts" in calls[0][0]
    assert calls[0][1]["shell"] is False
