from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

import src.openclaw_integration.core as core


def read_pipe_with_timeout(descriptor: int, size: int = 5, timeout: float = 5.0) -> bytes:
    readable, _, _ = select.select([descriptor], [], [], timeout)
    assert readable, "child process did not reach the expected checkpoint before timeout"
    return os.read(descriptor, size)


def integration_paths(tmp_path: Path) -> core.IntegrationPaths:
    home = tmp_path / "home"
    workspace = home / ".openclaw/workspace"
    paths = core.IntegrationPaths(
        home=home,
        workspace=workspace,
        project_root=workspace / "knowledge-lancedb-qwen-local",
        runtime_root=home / "Library/Application Support/OpenClaw/qwen-local",
        state_root=home / "Library/Application Support/OpenClaw/qwen-local-integration",
        launchd_plist=home / "Library/LaunchAgents/ai.openclaw.qwen-local-embedding.plist",
    )
    for directory in (
        paths.workspace, paths.project_root, paths.runtime_root, paths.state_root,
        paths.launchd_plist.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def cron_payload(jobs: list[dict[str, Any]], *, has_more: bool = False, total: int | None = None):
    return {"jobs": jobs, "total": len(jobs) if total is None else total, "hasMore": has_more, "nextCursor": None}


def job_for_spec(spec: core.ManagedCronSpec, *, job_id: str, enabled: bool) -> dict[str, Any]:
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
        "id": job_id,
        "name": spec.name,
        "description": spec.description,
        "enabled": enabled,
        "declarationKey": spec.key,
        "sessionTarget": spec.session_target,
        "sessionKey": None,
        "agentId": None,
        "deleteAfterRun": False,
        "schedule": {"kind": "cron", "expr": spec.schedule, "tz": spec.timezone, "staggerMs": 0},
        "payload": payload,
        "delivery": {"mode": "none"},
        "failureAlert": {
            "after": 1,
            "cooldownMs": 3600000,
            "includeSkipped": False,
            "mode": "announce",
            "channel": spec.report_channel,
            "to": spec.report_to,
            "accountId": spec.report_account_id,
        },
    }


def test_command_cron_alert_edit_never_sends_tools_patch(tmp_path: Path) -> None:
    item = manager(tmp_path)

    args = item._incremental_spec().alert_args("command-job")

    assert "--clear-tools" not in args
    assert "--tools" not in args


def test_command_cron_definition_rejects_unrestorable_tools_policy(tmp_path: Path) -> None:
    item = manager(tmp_path)
    job = job_for_spec(item._incremental_spec(), job_id="command-job", enabled=True)
    job["payload"]["toolsAllow"] = ["exec"]

    with pytest.raises(RuntimeError, match="tools"):
        core._job_definition(job)


def test_command_cron_restore_rejects_tools_before_cli_mutation(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    definition = job_for_spec(item._incremental_spec(), job_id="command-job", enabled=True)
    definition["payload"]["toolsAllow"] = ["exec"]

    with pytest.raises(RuntimeError, match="tools"):
        item._restore_cron_definition(definition, {})

    assert cli.calls == []


class StatefulCronCli:
    def __init__(self) -> None:
        self.executable = str(Path(sys.executable).resolve())
        self.jobs: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []
        self._next_job_sequence = 1
        self._used_job_ids: set[str] = set()

    def _allocate_job_id(self) -> str:
        self._used_job_ids.update(str(job["id"]) for job in self.jobs)
        while True:
            job_id = f"job-{self._next_job_sequence}"
            self._next_job_sequence += 1
            if job_id not in self._used_job_ids:
                self._used_job_ids.add(job_id)
                return job_id

    def json(self, args: list[str], *, timeout: int = 120) -> Any:
        self.calls.append(list(args))
        if args[:4] == ["cron", "list", "--all", "--json"]:
            return cron_payload(json.loads(json.dumps(self.jobs)))
        if args[:2] == ["cron", "add"]:
            key = args[args.index("--declaration-key") + 1]
            existing = next((job for job in self.jobs if job.get("declarationKey") == key), None)
            job_id = str(existing["id"]) if existing else self._allocate_job_id()
            env: dict[str, str] = {}
            for index, value in enumerate(args):
                if value == "--command-env":
                    name, setting = args[index + 1].split("=", 1)
                    env[name] = setting
            if "--cron" in args:
                schedule = {
                    "kind": "cron", "expr": args[args.index("--cron") + 1],
                    "tz": args[args.index("--tz") + 1],
                    "staggerMs": 0 if "--exact" in args else None,
                }
            elif "--at" in args:
                schedule = {"kind": "at", "at": args[args.index("--at") + 1]}
            elif "--every" in args:
                raw_every = args[args.index("--every") + 1]
                schedule = {"kind": "every", "everyMs": int(raw_every.removesuffix("ms"))}
            else:
                raise AssertionError(f"missing schedule in cron add: {args}")
            payload: dict[str, Any] = {
                "kind": "command",
                "argv": json.loads(args[args.index("--command-argv") + 1]),
                "cwd": args[args.index("--command-cwd") + 1],
                "timeoutSeconds": int(args[args.index("--timeout-seconds") + 1]),
                "noOutputTimeoutSeconds": int(args[args.index("--no-output-timeout-seconds") + 1]),
                "outputMaxBytes": int(args[args.index("--output-max-bytes") + 1]),
            }
            if env:
                payload["env"] = env
            if "--tools" in args:
                payload["toolsAllow"] = args[args.index("--tools") + 1].split(",")
            delivery: dict[str, Any] = {"mode": "none"}
            if "--announce" in args:
                delivery = {"mode": "announce"}
                for option, field in (("--channel", "channel"), ("--to", "to"), ("--account", "accountId")):
                    if option in args:
                        delivery[field] = args[args.index(option) + 1]
            job = {
                "id": job_id,
                "name": args[args.index("--name") + 1],
                "description": args[args.index("--description") + 1] if "--description" in args else None,
                "enabled": "--disabled" not in args,
                "declarationKey": key,
                "sessionTarget": args[args.index("--session") + 1] if "--session" in args else None,
                "sessionKey": args[args.index("--session-key") + 1] if "--session-key" in args else None,
                "agentId": args[args.index("--agent") + 1] if "--agent" in args else None,
                "wakeMode": args[args.index("--wake") + 1] if "--wake" in args else "now",
                "schedule": schedule,
                "payload": payload,
                "delivery": delivery,
            }
            if "--delete-after-run" in args:
                job["deleteAfterRun"] = True
            if existing:
                self.jobs[self.jobs.index(existing)] = job
            else:
                self.jobs.append(job)
            return {"id": job_id}
        raise AssertionError(f"unexpected json call: {args}")

    def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
        self.calls.append(list(args))
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0, "OpenClaw 2026.7.1-2\n", "")
        if args[:2] == ["cron", "rm"]:
            self._used_job_ids.add(args[2])
            self.jobs = [job for job in self.jobs if str(job["id"]) != args[2]]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["cron", "disable"]:
            job = next(item for item in self.jobs if item["id"] == args[2])
            job["enabled"] = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["cron", "edit"]:
            job = next(item for item in self.jobs if item["id"] == args[2])
            if job.get("payload", {}).get("kind") == "command" and (
                "--clear-tools" in args or "--tools" in args
            ):
                raise AssertionError("command cron edit must not patch tools")
            if "--failure-alert" in args:
                job["failureAlert"] = {
                    "after": 1,
                    "cooldownMs": 3600000,
                    "includeSkipped": "--failure-alert-include-skipped" in args,
                    "channel": args[args.index("--failure-alert-channel") + 1],
                }
                if "--failure-alert-mode" in args:
                    job["failureAlert"]["mode"] = args[args.index("--failure-alert-mode") + 1]
                if "--failure-alert-to" in args:
                    job["failureAlert"]["to"] = args[args.index("--failure-alert-to") + 1]
                if "--failure-alert-account-id" in args:
                    job["failureAlert"]["accountId"] = args[args.index("--failure-alert-account-id") + 1]
                job["enabled"] = False
            if "--description" in args:
                job["description"] = args[args.index("--description") + 1]
            if "--name" in args:
                job["name"] = args[args.index("--name") + 1]
            if "--enable" in args:
                job["enabled"] = True
            if "--disable" in args:
                job["enabled"] = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] in (["config", "validate"], ["gateway", "restart"]):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        raise AssertionError(f"unexpected run call: {args}")


def manager(tmp_path: Path, cli: Any | None = None, **kwargs: Any) -> core.IntegrationManager:
    report_channel = kwargs.pop("report_channel", "discord")
    report_to = kwargs.pop("report_to", "channel:1493072746702311474")
    report_account_id = kwargs.pop("report_account_id", "default")
    return core.IntegrationManager(
        paths=integration_paths(tmp_path),
        repo_root=Path(__file__).resolve().parents[1],
        cli=cli or StatefulCronCli(),
        node_path=Path(sys.executable),
        python_path=Path(sys.executable),
        report_channel=report_channel,
        report_to=report_to,
        report_account_id=report_account_id,
        **kwargs,
    )


def create_snapshot_root(item: core.IntegrationManager) -> bool:
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    return True


def legacy_snapshot_job(
    item: core.IntegrationManager,
    *,
    job_id: str = "legacy",
    declaration_key: str = core.LEGACY_SNAPSHOT_DECLARATION_KEY,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": core.LEGACY_SNAPSHOT_NAME,
        "description": core.LEGACY_SNAPSHOT_DESCRIPTION,
        "enabled": enabled,
        "declarationKey": declaration_key,
        "sessionTarget": "isolated",
        "sessionKey": None,
        "agentId": None,
        "deleteAfterRun": False,
        "schedule": {
            "kind": "cron", "expr": "50 6 * * *",
            "tz": item.timezone_name, "staggerMs": 0,
        },
        "payload": {
            "kind": "command",
            "argv": [
                "sh", "-lc", core._legacy_snapshot_shell_command(
                    project_root=item.paths.project_root,
                    snapshot_root=item.snapshot_root,
                    timezone_name=item.timezone_name,
                ),
            ],
            "cwd": str(item.paths.workspace),
            "timeoutSeconds": 7200,
            "noOutputTimeoutSeconds": 7200,
            "outputMaxBytes": 8192,
        },
        "delivery": {
            "mode": "announce", "channel": item.report_channel, "to": item.report_to,
        },
        "failureAlert": {
            "after": 1,
            "cooldownMs": 3600000,
            "includeSkipped": False,
            "channel": item.report_channel,
            "to": item.report_to,
        },
    }


def gemini_job(
    item: core.IntegrationManager,
    *,
    job_id: str = "gemini",
    enabled: bool = True,
    running: bool = False,
) -> dict[str, Any]:
    project = item.paths.workspace / "knowledge-lancedb"
    return {
        "id": job_id,
        "name": "Gemini local knowledge incremental index",
        "description": "Previously managed Gemini local index.",
        "enabled": enabled,
        "declarationKey": core.GEMINI_DECLARATION_KEY,
        "sessionTarget": "isolated",
        "sessionKey": None,
        "agentId": None,
        "deleteAfterRun": False,
        "schedule": {
            "kind": "cron", "expr": "15 6 * * *",
            "tz": item.timezone_name, "staggerMs": 0,
        },
        "payload": {
            "kind": "command",
            "argv": [str(project / "scripts/knowledge_index_incremental.sh")],
            "cwd": str(project),
            "timeoutSeconds": 7200,
            "noOutputTimeoutSeconds": 900,
            "outputMaxBytes": 65536,
        },
        "delivery": {"mode": "none"},
        "failureAlert": None,
        **({"state": {"status": "running"}} if running else {}),
    }


def approved_disabled_incremental_collision_job(
    item: core.IntegrationManager,
    *,
    job_id: str = "legacy-disabled-incremental",
    enabled: bool = False,
    declaration_key: str | None = None,
) -> dict[str, Any]:
    script = item.paths.project_root / "scripts/knowledge_index_incremental.sh"
    return {
        "id": job_id,
        "name": "LanceDB 知識庫每日增量索引",
        "description": None,
        "enabled": enabled,
        "declarationKey": declaration_key,
        "sessionTarget": "isolated",
        "sessionKey": None,
        "agentId": None,
        "deleteAfterRun": None,
        "schedule": {
            "kind": "cron", "expr": "30 6 * * *", "tz": item.timezone_name,
        },
        "payload": {
            "kind": "command",
            "argv": ["sh", "-lc", str(script)],
            "timeoutSeconds": 1800,
        },
        "delivery": {
            "mode": "announce", "channel": item.report_channel, "to": item.report_to,
        },
        "failureAlert": None,
    }


def collision_approval(job: dict[str, Any]) -> core.ApprovedDisabledCronCollision:
    return core.ApprovedDisabledCronCollision(
        job_id=str(job["id"]),
        contract_sha256=core._job_contract_hash(job, include_id=True),
        role="incremental",
    )


def test_complete_cron_inventory_rejects_pagination_count_and_duplicate_identities() -> None:
    one = {"id": "one", "declarationKey": "key-one"}
    two = {"id": "two", "declarationKey": "key-two"}
    assert core._cron_jobs(cron_payload([one, two])) == [one, two]

    with pytest.raises(RuntimeError, match="incomplete"):
        core._cron_jobs(cron_payload([one], has_more=True))
    offset_page = cron_payload([one])
    offset_page["offset"] = 1
    with pytest.raises(RuntimeError, match="incomplete"):
        core._cron_jobs(offset_page)
    next_page = cron_payload([one])
    next_page["nextOffset"] = 1
    with pytest.raises(RuntimeError, match="incomplete"):
        core._cron_jobs(next_page)
    with pytest.raises(RuntimeError, match="count"):
        core._cron_jobs(cron_payload([one], total=2))
    with pytest.raises(RuntimeError, match="duplicate"):
        core._cron_jobs(cron_payload([one, {**two, "id": "one"}]))
    with pytest.raises(RuntimeError, match="duplicate"):
        core._cron_jobs(cron_payload([one, {**two, "declarationKey": "key-one"}]))
    with pytest.raises(RuntimeError, match="completeness envelope"):
        core._cron_jobs([one, two])
    with pytest.raises(RuntimeError, match="invalid job id"):
        core._cron_jobs(cron_payload([{**one, "id": "--all"}]))

    empty_key = {"id": "empty-key", "declarationKey": ""}
    assert core._cron_jobs(cron_payload([empty_key])) == [empty_key]


def test_quiescence_disables_and_waits_for_managed_and_exact_gemini_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ActiveStateCli(StatefulCronCli):
        def __init__(self) -> None:
            super().__init__()
            self.list_reads = 0

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:4] == ["cron", "list", "--all", "--json"]:
                self.list_reads += 1
                if self.list_reads >= 3:
                    for job in self.jobs:
                        job.pop("state", None)
            return super().json(args, timeout=timeout)

    cli = ActiveStateCli()
    item = manager(tmp_path, cli)
    incremental = job_for_spec(item._incremental_spec(), job_id="incremental", enabled=True)
    incremental["state"] = {"status": "running"}
    gemini = gemini_job(item, running=True)
    jobs_before = [incremental, gemini]
    cli.jobs = jobs_before
    hashes = item._inventory_hashes(jobs_before)
    monkeypatch.setattr(core.time, "sleep", lambda _: None)

    disabled = item._quiesce_prior_jobs(jobs_before, {"incremental", "gemini"}, hashes)

    assert disabled == ["gemini", "incremental"]
    assert cli.list_reads >= 4
    assert all(job["enabled"] is False and "state" not in job for job in cli.jobs)
    assert ["cron", "edit", "gemini", "--disable"] in cli.calls
    assert ["cron", "edit", "incremental", "--disable"] in cli.calls


def test_same_key_upgrade_removes_exact_owned_ids_before_strict_add(
    tmp_path: Path,
) -> None:
    class StrictDuplicateKeyCli(StatefulCronCli):
        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:2] == ["cron", "add"]:
                key = args[args.index("--declaration-key") + 1]
                if any(job.get("declarationKey") == key for job in self.jobs):
                    raise RuntimeError("duplicate declaration key")
            return super().json(args, timeout=timeout)

    cli = StrictDuplicateKeyCli()
    item = manager(tmp_path, cli)
    prior_incremental = job_for_spec(
        item._incremental_spec(), job_id="prior-incremental", enabled=True,
    )
    unknown = customer_job()
    jobs_before = [prior_incremental, unknown]
    cli.jobs = json.loads(json.dumps(jobs_before))
    hashes = item._inventory_hashes(jobs_before)
    targets = {"prior-incremental"}
    transaction = write_staging_transaction(item, jobs_before, targets)

    item._quiesce_prior_jobs(jobs_before, targets, hashes)
    with pytest.raises(RuntimeError, match="appeared before authorized add"):
        item._apply_managed_spec(item._incremental_spec(), transaction)

    removed = item._remove_prior_managed_jobs_for_replacement(
        jobs_before, targets, hashes,
    )
    replacement_id = item._apply_managed_spec(item._incremental_spec(), transaction)

    assert removed == ["prior-incremental"]
    assert replacement_id != "prior-incremental"
    assert any(job["id"] == replacement_id for job in cli.jobs)
    after_unknown = next(job for job in cli.jobs if job["id"] == "customer")
    assert core._job_contract_hash(after_unknown, include_id=True) == core._job_contract_hash(
        unknown, include_id=True,
    )
    assert ["cron", "rm", "prior-incremental"] in cli.calls


def test_integrate_replaces_same_key_jobs_from_write_ahead_receipt_before_strict_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StrictDuplicateKeyCli(StatefulCronCli):
        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:2] == ["cron", "add"]:
                key = args[args.index("--declaration-key") + 1]
                if any(job.get("declarationKey") == key for job in self.jobs):
                    raise RuntimeError("duplicate declaration key")
            return super().json(args, timeout=timeout)

    cli = StrictDuplicateKeyCli()
    item = manager(tmp_path, cli)
    prior_incremental = job_for_spec(
        item._incremental_spec(), job_id="prior-incremental", enabled=True,
    )
    prior_snapshot = job_for_spec(
        item._snapshot_spec(), job_id="prior-snapshot", enabled=True,
    )
    unknown = customer_job()
    cli.jobs = json.loads(json.dumps([prior_incremental, prior_snapshot, unknown]))
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "prior-install",
        "phase": "committed",
        "ownership": item._ownership_payload(),
        "indexState": "READY",
        "cronId": "prior-incremental",
        "snapshotCronId": "prior-snapshot",
        "initialIndexJobId": None,
        "cronDefinitionsBefore": [],
        "cronTargetIdsBefore": [],
        "cronInventoryHashesBefore": {
            "customer": core._legacy_v3_job_contract_hash(unknown, include_id=True),
        },
        "cronUnknownHashesBefore": {
            "customer": core._legacy_v3_job_contract_hash(unknown, include_id=True),
        },
        "disabledGeminiJobs": [],
    })
    prepare_collision_integration_runtime(item, monkeypatch, run_id="same-key-upgrade")

    result = item._integrate_locked({"runtimePort": 18888})

    assert result["transaction"] == "upgraded"
    transaction = item.store.read()
    expected_ids = ["prior-incremental", "prior-snapshot"]
    assert transaction["phase"] == "committed"
    assert transaction["previousContractVersion"] == core.INTEGRATION_CONTRACT_VERSION
    assert transaction["cronCommitTopologyVerified"] is True
    assert transaction["cronCommitInventoryHashes"] == item._inventory_hashes(cli.jobs)
    assert transaction["cronCommitUnknownHashes"] == transaction["cronUnknownHashesBefore"]
    assert transaction["cronReplaceIdsBefore"] == expected_ids
    assert transaction["removedManagedCronIdsBeforeAdd"] == expected_ids
    definitions = {
        str(definition["id"]): definition
        for definition in transaction["cronDefinitionsBefore"]
    }
    assert set(definitions) == set(expected_ids)
    assert transaction["cronInventoryHashesBefore"]["prior-incremental"] == (
        core._job_contract_hash(prior_incremental, include_id=True)
    )
    assert transaction["cronInventoryHashesBefore"]["prior-snapshot"] == (
        core._job_contract_hash(prior_snapshot, include_id=True)
    )
    first_add = next(
        index for index, call in enumerate(cli.calls) if call[:2] == ["cron", "add"]
    )
    removal_indexes = [
        index for index, call in enumerate(cli.calls)
        if call[:2] == ["cron", "rm"] and call[2] in expected_ids
    ]
    assert len(removal_indexes) == 2 and max(removal_indexes) < first_add
    assert not any(call[:3] == ["cron", "rm", "customer"] for call in cli.calls)
    after_unknown = next(job for job in cli.jobs if job["id"] == "customer")
    assert core._job_contract_hash(after_unknown, include_id=True) == core._job_contract_hash(
        unknown, include_id=True,
    )
    assert item._job_by_key(cli.jobs, core.CRON_DECLARATION_KEY)["enabled"] is True
    assert item._job_by_key(cli.jobs, core.SNAPSHOT_CRON_DECLARATION_KEY)["enabled"] is True
    cli.jobs = [job for job in cli.jobs if job["id"] != "customer"]
    later_unrelated = customer_job(job_id="later-unrelated")
    later_unrelated["declarationKey"] = "later-unrelated-v1"
    cli.jobs.append(later_unrelated)
    assert item.verify()["ok"] is True

    incremental_after = item._job_by_key(cli.jobs, core.CRON_DECLARATION_KEY)
    assert incremental_after is not None
    committed_incremental_id = str(incremental_after["id"])
    incremental_after["id"] = "replacement-same-contract"
    with pytest.raises(RuntimeError, match="id does not match the committed receipt"):
        item.verify()
    incremental_after["id"] = committed_incremental_id

    cli.jobs.append(initial_job(item))
    with pytest.raises(RuntimeError, match="Unexpected initial cron exists"):
        item.verify()
    cli.jobs = [job for job in cli.jobs if job["id"] != "initial"]

    invalid = item.store.read()
    invalid["indexState"] = "UNKNOWN"
    item.store.write(invalid)
    with pytest.raises(RuntimeError, match="index state is invalid"):
        item.verify()

    invalid["indexState"] = "READY"
    invalid["initialIndexJobId"] = "stale-initial-id"
    item.store.write(invalid)
    with pytest.raises(RuntimeError, match="Unexpected initial cron exists"):
        item.verify()


def test_managed_replacement_preserves_nonmanaged_targets_and_unknown_jobs(
    tmp_path: Path,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    incremental = job_for_spec(
        item._incremental_spec(), job_id="managed-incremental", enabled=True,
    )
    snapshot = job_for_spec(
        item._snapshot_spec(), job_id="managed-snapshot", enabled=True,
    )
    legacy = legacy_snapshot_job(item, job_id="legacy-snapshot", enabled=True)
    gemini = gemini_job(item, job_id="gemini", enabled=True)
    unknown = customer_job()
    jobs_before = [incremental, snapshot, legacy, gemini, unknown]
    cli.jobs = json.loads(json.dumps(jobs_before))
    hashes = item._inventory_hashes(jobs_before)
    targets = {"managed-incremental", "managed-snapshot", "legacy-snapshot", "gemini"}
    write_staging_transaction(item, jobs_before, targets)

    item._quiesce_prior_jobs(jobs_before, targets, hashes)
    removed = item._remove_prior_managed_jobs_for_replacement(
        jobs_before, targets, hashes,
    )

    assert removed == ["managed-incremental", "managed-snapshot"]
    by_id = {str(job["id"]): job for job in cli.jobs}
    assert set(by_id) == {"legacy-snapshot", "gemini", "customer"}
    assert by_id["legacy-snapshot"]["enabled"] is False
    assert by_id["gemini"]["enabled"] is False
    assert core._job_contract_hash(by_id["customer"], include_id=True) == core._job_contract_hash(
        unknown, include_id=True,
    )
    removed_by_cli = [call[2] for call in cli.calls if call[:2] == ["cron", "rm"]]
    assert removed_by_cli == ["managed-incremental", "managed-snapshot"]


def test_managed_replacement_blocks_inventory_drift_before_remove(
    tmp_path: Path,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    incremental = job_for_spec(
        item._incremental_spec(), job_id="managed-incremental", enabled=True,
    )
    unknown = customer_job()
    jobs_before = [incremental, unknown]
    cli.jobs = json.loads(json.dumps(jobs_before))
    hashes = item._inventory_hashes(jobs_before)
    targets = {"managed-incremental"}
    write_staging_transaction(item, jobs_before, targets)
    item._quiesce_prior_jobs(jobs_before, targets, hashes)
    cli.jobs[1]["description"] = "drifted after quiescence"

    with pytest.raises(RuntimeError, match="changed before managed replacement"):
        item._remove_prior_managed_jobs_for_replacement(jobs_before, targets, hashes)

    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)
    assert any(job["id"] == "managed-incremental" for job in cli.jobs)


def test_crash_after_first_managed_remove_recovers_from_write_ahead_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    class CrashOnceAfterRemoveCli(StatefulCronCli):
        crashed = False

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            result = super().run(args, timeout=timeout, check=check)
            if args[:2] == ["cron", "rm"] and not self.crashed:
                self.crashed = True
                raise SimulatedCrash("process stopped after cron removal")
            return result

    cli = CrashOnceAfterRemoveCli()
    item = manager(tmp_path, cli)
    incremental = job_for_spec(
        item._incremental_spec(), job_id="managed-incremental", enabled=True,
    )
    snapshot = job_for_spec(
        item._snapshot_spec(), job_id="managed-snapshot", enabled=True,
    )
    unknown = customer_job()
    jobs_before = [incremental, snapshot, unknown]
    hashes = item._inventory_hashes(jobs_before)
    targets = {"managed-incremental", "managed-snapshot"}
    cli.jobs = json.loads(json.dumps(jobs_before))
    item._quiesce_prior_jobs(jobs_before, targets, hashes)
    write_rollback_transaction(
        item,
        prior_definitions=[core._job_definition(incremental), core._job_definition(snapshot)],
        unknown=unknown,
        target_ids=sorted(targets),
        managed_after=[],
    )
    transaction = item.store.read()
    transaction["phase"] = "replacing_managed_cron"
    transaction["cronInventoryTotalBefore"] = len(jobs_before)
    transaction["disabledGeminiJobs"] = []
    transaction["cronReplaceIdsBefore"] = sorted(targets)
    item.store.write(transaction)

    with pytest.raises(SimulatedCrash):
        item._remove_prior_managed_jobs_for_replacement(jobs_before, targets, hashes)

    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)
    result = item._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    rolled_back = item.store.read()
    assert rolled_back["phase"] == "rolled_back"
    assert rolled_back["rollbackOriginPhase"] == "replacing_managed_cron"
    assert item._validate_replacement_receipt_graph(rolled_back) is True
    restored = [job for job in cli.jobs if job["id"] != "customer"]
    assert sorted(core._job_contract_hash(job) for job in restored) == sorted([
        core._job_contract_hash(incremental), core._job_contract_hash(snapshot),
    ])
    after_unknown = next(job for job in cli.jobs if job["id"] == "customer")
    assert core._job_contract_hash(after_unknown, include_id=True) == core._job_contract_hash(
        unknown, include_id=True,
    )


def test_crash_after_first_replacement_add_recovers_without_created_id_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    class CrashAfterCreatedAddCli(StatefulCronCli):
        crashed = False

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            result = super().json(args, timeout=timeout)
            if args[:2] == ["cron", "add"] and not self.crashed:
                self.crashed = True
                raise SimulatedCrash("process stopped after cron add")
            return result

    cli = CrashAfterCreatedAddCli()
    item = manager(tmp_path, cli)
    incremental = job_for_spec(
        item._incremental_spec(), job_id="managed-incremental", enabled=True,
    )
    snapshot = job_for_spec(
        item._snapshot_spec(), job_id="managed-snapshot", enabled=True,
    )
    unknown = customer_job()
    jobs_before = [incremental, snapshot, unknown]
    hashes = item._inventory_hashes(jobs_before)
    targets = {"managed-incremental", "managed-snapshot"}
    cli.jobs = json.loads(json.dumps(jobs_before))
    item._quiesce_prior_jobs(jobs_before, targets, hashes)
    write_rollback_transaction(
        item,
        prior_definitions=[core._job_definition(incremental), core._job_definition(snapshot)],
        unknown=unknown,
        target_ids=sorted(targets),
        managed_after=[],
    )
    transaction = item.store.read()
    transaction["phase"] = "replacing_managed_cron"
    transaction["cronInventoryTotalBefore"] = len(jobs_before)
    transaction["disabledGeminiJobs"] = []
    transaction["cronReplaceIdsBefore"] = sorted(targets)
    item.store.write(transaction)
    item._remove_prior_managed_jobs_for_replacement(jobs_before, targets, hashes)
    with pytest.raises(SimulatedCrash):
        item._apply_managed_spec(item._incremental_spec(), transaction)
    transaction = item.store.read()
    intent = transaction["cronStagingIntents"][core.CRON_DECLARATION_KEY]
    assert "jobId" not in intent
    assert transaction.get("managedCronIdsAfter", []) == []
    staged = next(job for job in cli.jobs if job["id"] != "customer")
    replacement_id = str(staged["id"])
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)
    result = item._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    restored = [job for job in cli.jobs if job["id"] != "customer"]
    assert sorted(core._job_contract_hash(job) for job in restored) == sorted([
        core._job_contract_hash(incremental), core._job_contract_hash(snapshot),
    ])
    assert all(job["id"] != replacement_id for job in cli.jobs)
    after_unknown = next(job for job in cli.jobs if job["id"] == "customer")
    assert core._job_contract_hash(after_unknown, include_id=True) == core._job_contract_hash(
        unknown, include_id=True,
    )


@pytest.mark.parametrize("fault", ["changed-customer", "extra-unrelated"])
def test_success_inventory_drift_fails_without_deleting_unreceipted_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    class DriftAfterRecurringAddsCli(StatefulCronCli):
        add_count = 0

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            result = super().json(args, timeout=timeout)
            if args[:2] == ["cron", "add"]:
                self.add_count += 1
                if self.add_count == 2:
                    if fault == "changed-customer":
                        customer = next(job for job in self.jobs if job["id"] == "customer")
                        customer["description"] = "concurrent customer change"
                    else:
                        self.jobs.append(customer_job(job_id="concurrent-extra"))
            return result

    case_root = tmp_path / fault
    cli = DriftAfterRecurringAddsCli()
    item = manager(case_root, cli)
    incremental = job_for_spec(
        item._incremental_spec(), job_id="prior-incremental", enabled=True,
    )
    snapshot = job_for_spec(
        item._snapshot_spec(), job_id="prior-snapshot", enabled=True,
    )
    unknown = customer_job()
    cli.jobs = json.loads(json.dumps([incremental, snapshot, unknown]))
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": 1,
        "runId": "prior-install",
        "phase": "committed",
        "ownership": {"schema": "qwen-local-openclaw.v1"},
    })
    prepare_collision_integration_runtime(item, monkeypatch, run_id=fault)

    with pytest.raises(core.IntegrationRollbackIncomplete) as caught:
        item._integrate_locked({"runtimePort": 18888})

    assert "cron" in str(caught.value.original_error).lower()
    protected_id = "customer" if fault == "changed-customer" else "concurrent-extra"
    assert any(job["id"] == protected_id for job in cli.jobs)
    assert not any(call[:3] == ["cron", "rm", protected_id] for call in cli.calls)
    assert item.store.read()["phase"] == "rollback_failed"


def test_hostile_concurrent_same_key_job_is_never_deleted_by_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileAfterRemovalCli(StatefulCronCli):
        hostile: dict[str, Any] | None = None

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            result = super().run(args, timeout=timeout, check=check)
            if args[:3] == ["cron", "rm", "prior-snapshot"]:
                assert self.hostile is not None
                self.jobs.append(json.loads(json.dumps(self.hostile)))
            return result

    cli = HostileAfterRemovalCli()
    item = manager(tmp_path, cli)
    incremental = job_for_spec(
        item._incremental_spec(), job_id="prior-incremental", enabled=True,
    )
    snapshot = job_for_spec(
        item._snapshot_spec(), job_id="prior-snapshot", enabled=True,
    )
    unknown = customer_job()
    cli.hostile = job_for_spec(
        item._incremental_spec(), job_id="hostile-same-key", enabled=False,
    )
    cli.jobs = json.loads(json.dumps([incremental, snapshot, unknown]))
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": 1,
        "runId": "prior-install",
        "phase": "committed",
        "ownership": {"schema": "qwen-local-openclaw.v1"},
    })
    prepare_collision_integration_runtime(item, monkeypatch, run_id="hostile-same-key")

    with pytest.raises(core.IntegrationRollbackIncomplete):
        item._integrate_locked({"runtimePort": 18888})

    assert any(job["id"] == "hostile-same-key" for job in cli.jobs)
    assert not any(
        call[:3] == ["cron", "rm", "hostile-same-key"] for call in cli.calls
    )


def test_uncheckpointed_staging_intent_requires_zero_or_one_exact_match(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    spec = item._incremental_spec()
    transaction = write_staging_transaction(item, [], set())
    intent = item._ensure_cron_intent(
        transaction,
        bucket_name="cronStagingIntents",
        declaration_key=spec.key,
        canonical_description=spec.description,
        role="managed",
        expected_factory=lambda description: item._managed_pre_alert_definition(
            spec, description,
        ),
    )

    assert item._uncheckpointed_intent_candidate(intent, []) is None
    exact = item._managed_pre_alert_definition(spec, intent["stagingDescription"])
    exact["id"] = "candidate-one"
    duplicate = json.loads(json.dumps(exact))
    duplicate["id"] = "candidate-two"
    with pytest.raises(RuntimeError, match="ambiguous"):
        item._uncheckpointed_intent_candidate(intent, [exact, duplicate])
    drifted = json.loads(json.dumps(exact))
    drifted["payload"]["timeoutSeconds"] += 1
    with pytest.raises(RuntimeError, match="contract drifted"):
        item._uncheckpointed_intent_candidate(intent, [drifted])


def test_initial_add_crash_before_id_checkpoint_adopts_unique_staged_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    class CrashOnceInitialCli(StatefulCronCli):
        crashed = False

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            result = super().json(args, timeout=timeout)
            if args[:2] == ["cron", "add"] and not self.crashed:
                self.crashed = True
                raise SimulatedCrash("initial add response lost")
            return result

    cli = CrashOnceInitialCli()
    item = manager(tmp_path, cli)
    full_script = item.paths.project_root / "scripts/knowledge_index_full.sh"
    full_script.parent.mkdir(parents=True, exist_ok=True)
    full_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    full_script.chmod(0o700)
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", ""),
    )
    transaction = write_staging_transaction(item, [], set())

    with pytest.raises(SimulatedCrash):
        item.mark_ready_or_schedule_build(transaction)

    persisted = item.store.read()
    intent = persisted["cronStagingIntents"][core.INITIAL_CRON_DECLARATION_KEY]
    assert "jobId" not in intent
    staged_id = str(cli.jobs[0]["id"])
    state, recovered_id = item.mark_ready_or_schedule_build(persisted)

    assert (state, recovered_id) == ("INDEX_BUILDING", staged_id)
    assert item.store.read()["cronStagingIntents"][
        core.INITIAL_CRON_DECLARATION_KEY
    ]["jobId"] == staged_id
    assert sum(call[:2] == ["cron", "add"] for call in cli.calls) == 1
    assert cli.jobs[0]["enabled"] is False
    assert cli.jobs[0]["description"] == core.INITIAL_CRON_DESCRIPTION


def test_rollback_restore_add_crash_resumes_exact_staging_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    class CrashOnceRestoreCli(StatefulCronCli):
        crashed = False

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            result = super().json(args, timeout=timeout)
            if args[:2] == ["cron", "add"] and not self.crashed:
                self.crashed = True
                raise SimulatedCrash("restore add response lost")
            return result

    cli = CrashOnceRestoreCli()
    item = manager(tmp_path, cli)
    prior = job_for_spec(item._incremental_spec(), job_id="prior", enabled=True)
    unknown = customer_job()
    cli.jobs = [json.loads(json.dumps(unknown))]
    write_rollback_transaction(
        item,
        prior_definitions=[core._job_definition(prior)],
        unknown=unknown,
        target_ids=["prior"],
        managed_after=[],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    with pytest.raises(SimulatedCrash):
        item._rollback_locked(require_exact_post_config=False)

    transaction = item.store.read()
    intent = transaction["cronRestoreIntents"][core.CRON_DECLARATION_KEY]
    assert "jobId" not in intent
    staged_id = next(job["id"] for job in cli.jobs if job["id"] != "customer")
    result = item._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    assert item.store.read()["restoredCronIdsByDeclaration"][
        core.CRON_DECLARATION_KEY
    ] == staged_id
    assert sum(call[:2] == ["cron", "add"] for call in cli.calls) == 1
    after_unknown = next(job for job in cli.jobs if job["id"] == "customer")
    assert core._job_contract_hash(after_unknown, include_id=True) == (
        core._job_contract_hash(unknown, include_id=True)
    )


def test_cron_edits_observe_durable_id_checkpoint_and_never_transiently_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CheckpointGuardCli(StatefulCronCli):
        item: core.IntegrationManager | None = None

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            if args[:2] == ["cron", "edit"] and (
                "--failure-alert" in args or "--description" in args
            ):
                assert self.item is not None
                transaction = self.item.store.read()
                job = next(entry for entry in self.jobs if entry["id"] == args[2])
                intent = next(
                    value for bucket in (
                        transaction.get("cronStagingIntents", {}),
                        transaction.get("cronRestoreIntents", {}),
                    )
                    for value in bucket.values()
                    if value.get("jobId") == args[2]
                )
                assert intent["jobId"] == args[2]
                assert job["enabled"] is False
                assert "--disable" in args
            result = super().run(args, timeout=timeout, check=check)
            if args[:2] == ["cron", "edit"] and "--description" in args:
                job = next(entry for entry in self.jobs if entry["id"] == args[2])
                assert job["enabled"] is False
            return result

    cli = CheckpointGuardCli()
    item = manager(tmp_path, cli)
    cli.item = item
    transaction = write_staging_transaction(item, [], set())

    job_id = item._apply_managed_spec(item._incremental_spec(), transaction)
    restored_id = item._restore_cron_definition(
        core._job_definition(legacy_snapshot_job(item)), transaction,
    )
    full_script = item.paths.project_root / "scripts/knowledge_index_full.sh"
    full_script.parent.mkdir(parents=True, exist_ok=True)
    full_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    full_script.chmod(0o700)
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", ""),
    )
    state, initial_id = item.mark_ready_or_schedule_build(transaction)

    assert item.store.read()["cronStagingIntents"][
        core.CRON_DECLARATION_KEY
    ]["jobId"] == job_id
    assert item.store.read()["restoredCronIdsByDeclaration"][
        core.LEGACY_SNAPSHOT_DECLARATION_KEY
    ] == restored_id
    assert state == "INDEX_BUILDING" and initial_id is not None
    assert all(job["enabled"] is False for job in cli.jobs if job["id"] != restored_id)


def test_integration_includes_exact_gemini_job_in_quiescence_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = manager(tmp_path)
    gemini = gemini_job(item)
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    base = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "gemini-target",
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "projectExisted": True,
        "healthReceiptExisted": False,
    }
    captured: set[str] = set()
    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([gemini], []))
    monkeypatch.setattr(item, "begin", lambda **_: dict(base))

    def stop_after_capture(
        _jobs: list[dict[str, Any]], target_ids: set[str], _hashes: dict[str, str]
    ) -> list[str]:
        captured.update(target_ids)
        raise RuntimeError("fixture stop after quiescence target capture")

    monkeypatch.setattr(item, "_quiesce_prior_jobs", stop_after_capture)
    monkeypatch.setattr(item, "_rollback_locked", lambda **_: {"ok": True})

    with pytest.raises(RuntimeError, match="fixture stop"):
        item._integrate_locked({"runtimePort": 18888})

    assert captured == {"gemini"}


def test_integration_persists_creation_intent_without_claiming_uncreated_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    item.paths.project_root.rmdir()
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    base = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "planned-not-created",
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "projectExisted": False,
        "healthReceiptExisted": False,
    }
    captured: dict[str, Any] = {}
    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "begin", lambda **_: dict(base))

    def stop_before_creation(*_args: Any) -> list[str]:
        captured.update(item.store.read())
        raise RuntimeError("fixture stop before root creation")

    monkeypatch.setattr(item, "_quiesce_prior_jobs", stop_before_creation)
    monkeypatch.setattr(item, "_rollback_locked", lambda **_: {"ok": True})

    with pytest.raises(RuntimeError, match="fixture stop"):
        item._integrate_locked({"runtimePort": 18888})

    assert captured["snapshotRootCreatePlanned"] is True
    assert captured["snapshotRootCreated"] is False
    assert captured["projectCreatePlanned"] is True
    assert captured["projectCreated"] is False
    assert not item.snapshot_root.exists()
    assert not item.paths.project_root.exists()


def test_atomic_capability_failure_happens_before_cron_or_runtime_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    base = {
        "schemaVersion": core.SCHEMA_VERSION,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "capability-failure",
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "projectExisted": True,
        "healthReceiptExisted": False,
    }
    quiesce_called = False
    rollback_called = False
    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "begin", lambda **_: dict(base))

    def fail_probe(*_args: Any, **_kwargs: Any) -> None:
        receipt = item.store.read()
        assert receipt["cronMutationStarted"] is False
        assert receipt["runtimeMutationStarted"] is False
        raise RuntimeError("fixture unsupported filesystem")

    def quiesce(*_args: Any, **_kwargs: Any) -> list[str]:
        nonlocal quiesce_called
        quiesce_called = True
        return []

    def rollback(**_kwargs: Any) -> dict[str, Any]:
        nonlocal rollback_called
        rollback_called = True
        return {"ok": True}

    monkeypatch.setattr(item, "_probe_atomic_publication_capability", fail_probe)
    monkeypatch.setattr(item, "_quiesce_prior_jobs", quiesce)
    monkeypatch.setattr(item, "_rollback_locked", rollback)

    with pytest.raises(RuntimeError, match="unsupported filesystem"):
        item._integrate_locked({"runtimePort": 18888})

    assert quiesce_called is False
    assert rollback_called is True
    assert item.store.read()["cronMutationStarted"] is False


def test_runtime_quiescence_guard_refuses_held_index_lock_without_mutation(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    index_lock = data / "index.lock"
    index_lock.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="index run did not quiesce"):
        with item._runtime_quiescence_guard(
            timeout_seconds=0, poll_seconds=0.01, checkpoint=lambda _receipt: None,
        ):
            pytest.fail("guard must not yield while the index lock is held")

    assert index_lock.is_dir()
    assert not item.snapshot_root.exists()


def test_runtime_quiescence_guard_refuses_held_snapshot_lock_and_releases_index_lock(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    snapshot_lock = item.snapshot_root / ".snapshot-run.lock"
    descriptor = os.open(snapshot_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        core.fcntl.flock(descriptor, core.fcntl.LOCK_EX | core.fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="snapshot run did not quiesce"):
            with item._runtime_quiescence_guard(
                timeout_seconds=0, poll_seconds=0.01, checkpoint=lambda _receipt: None,
            ):
                pytest.fail("guard must not yield while the snapshot lock is held")
    finally:
        core.fcntl.flock(descriptor, core.fcntl.LOCK_UN)
        os.close(descriptor)

    assert snapshot_lock.is_file()
    assert not (item.paths.project_root / "data/index.lock").exists()


def test_runtime_quiescence_guard_receipts_created_index_lock_identity(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    item.snapshot_root.mkdir(parents=True, mode=0o700)

    with item._runtime_quiescence_guard(checkpoint=lambda _receipt: None) as receipt:
        index_lock = data / "index.lock"
        metadata = index_lock.stat()
        assert receipt["indexLockCreated"] is True
        assert receipt["indexLockPublished"] is True
        assert core.INDEX_LOCK_STAGE_RE.fullmatch(receipt["indexLockStageName"])
        assert receipt["indexLockStageDev"] == metadata.st_dev
        assert receipt["indexLockStageIno"] == metadata.st_ino
        assert receipt["indexLockDev"] == metadata.st_dev
        assert receipt["indexLockIno"] == metadata.st_ino

    assert not (data / "index.lock").exists()


def test_atomic_noreplace_publication_preserves_existing_target(tmp_path: Path) -> None:
    parent = tmp_path / "locks"
    parent.mkdir(mode=0o700)
    stage = parent / ".index.lock.install-00000000000000000000000000000000"
    final = parent / "index.lock"
    stage.mkdir(mode=0o700)
    final.mkdir(mode=0o700)
    stage_identity = stage.stat()
    final_identity = final.stat()
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(FileExistsError):
            core._rename_noreplace_at(parent_fd, stage.name, parent_fd, final.name)
    finally:
        os.close(parent_fd)

    assert (stage.stat().st_dev, stage.stat().st_ino) == (
        stage_identity.st_dev, stage_identity.st_ino,
    )
    assert (final.stat().st_dev, final.stat().st_ino) == (
        final_identity.st_dev, final_identity.st_ino,
    )


@pytest.mark.parametrize(
    ("platform", "symbol", "expected_flag"),
    [("darwin", "renameatx_np", 0x00000004), ("linux", "renameat2", 0x00000001)],
)
def test_atomic_noreplace_uses_platform_abi_and_expected_flag(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    symbol: str,
    expected_flag: int,
) -> None:
    class Operation:
        argtypes: list[Any] | None = None
        restype: Any = None

        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def __call__(self, *args: Any) -> int:
            self.calls.append(args)
            return 0

    operation = Operation()
    library = type("FakeLibc", (), {})()
    setattr(library, symbol, operation)
    monkeypatch.setattr(core.sys, "platform", platform)
    monkeypatch.setattr(core.ctypes, "CDLL", lambda *_args, **_kwargs: library)

    core._rename_noreplace_at(11, "stage", 12, "target")

    assert operation.calls == [(11, b"stage", 12, b"target", expected_flag)]
    assert operation.restype is core.ctypes.c_int
    assert operation.argtypes == [
        core.ctypes.c_int,
        core.ctypes.c_char_p,
        core.ctypes.c_int,
        core.ctypes.c_char_p,
        core.ctypes.c_uint,
    ]


@pytest.mark.parametrize(
    ("error_number", "exception", "message"),
    [
        (core.errno.EEXIST, FileExistsError, None),
        (core.errno.ENOTEMPTY, FileExistsError, None),
        (core.errno.ENOSYS, RuntimeError, "unavailable on this filesystem"),
        (core.errno.EOPNOTSUPP, RuntimeError, "unavailable on this filesystem"),
        (core.errno.EINVAL, RuntimeError, "unavailable on this filesystem"),
        (core.errno.EIO, OSError, None),
    ],
)
def test_atomic_noreplace_maps_native_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    exception: type[BaseException],
    message: str | None,
) -> None:
    class Operation:
        argtypes: list[Any] | None = None
        restype: Any = None

        def __call__(self, *_args: Any) -> int:
            return -1

    library = type("FakeLibc", (), {"renameat2": Operation()})()
    monkeypatch.setattr(core.sys, "platform", "linux")
    monkeypatch.setattr(core.ctypes, "CDLL", lambda *_args, **_kwargs: library)
    monkeypatch.setattr(core.ctypes, "get_errno", lambda: error_number)

    context = pytest.raises(exception, match=message) if message else pytest.raises(exception)
    with context:
        core._rename_noreplace_at(11, "stage", 12, "target")


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_atomic_noreplace_fails_closed_when_native_symbol_or_platform_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    monkeypatch.setattr(core.sys, "platform", platform)
    monkeypatch.setattr(core.ctypes, "CDLL", lambda *_args, **_kwargs: object())

    with pytest.raises(RuntimeError, match="unavailable on this platform"):
        core._rename_noreplace_at(11, "stage", 12, "target")


def test_fresh_custom_snapshot_parent_uses_nearest_existing_safe_ancestor(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    custom_parent = item.paths.home / "OpenClawBackups/customer"
    assert not custom_parent.exists()

    selected = item._nearest_existing_capability_parent(custom_parent)

    assert selected == item.paths.home
    transaction: dict[str, Any] = {"phase": "preflight_complete"}
    item.store.write(transaction)
    item._probe_atomic_publication_capability(
        transaction,
        parent=selected,
        receipt_prefix="snapshotParentCapabilityProbe",
    )
    assert transaction["snapshotParentCapabilityProbeParent"] == str(item.paths.home)
    assert transaction["snapshotParentCapabilityProbePublished"] is True
    assert transaction["snapshotParentCapabilityProbeQuarantined"] is True
    assert transaction["snapshotParentCapabilityProbeQuarantinePurged"] is True
    assert not (
        item.paths.home / transaction["snapshotParentCapabilityProbeQuarantineName"]
    ).exists()

    custom_parent.mkdir(parents=True, mode=0o700)
    recovered = item._capability_parent_from_transaction(
        transaction,
        receipt_prefix="snapshotParentCapabilityProbe",
        target_parent=custom_parent,
    )
    assert recovered == item.paths.home


def test_capability_parent_receipt_must_be_an_ancestor_of_the_owned_target(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    unrelated = item.paths.home / "unrelated"
    unrelated.mkdir(mode=0o700)
    transaction = {"probeParent": str(unrelated)}

    with pytest.raises(RuntimeError, match="not an ancestor"):
        item._capability_parent_from_transaction(
            transaction,
            receipt_prefix="probe",
            target_parent=item.paths.workspace / "snapshots",
        )


def test_capability_recovery_purges_exact_interrupted_stage(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    stage_name = ".qwen-capability-probe-11111111111111111111111111111111"
    final_name = ".qwen-capability-probe-published-22222222222222222222222222222222"
    stage = item.paths.home / stage_name
    stage.mkdir(mode=0o700)
    metadata = stage.stat()
    transaction = {
        "schemaVersion": core.SCHEMA_VERSION,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "probeParent": str(item.paths.home),
        "probeStageName": stage_name,
        "probeStageDev": metadata.st_dev,
        "probeStageIno": metadata.st_ino,
        "probeFinalName": final_name,
        "probePublished": False,
    }
    item.store.write(transaction)

    item._recover_capability_probe(
        transaction,
        receipt_prefix="probe",
        expected_parent=item.paths.home,
    )

    assert not stage.exists()
    assert transaction["probeQuarantined"] is True
    assert transaction["probeQuarantinePurged"] is True
    assert transaction["probePreserved"] is False
    assert not (item.paths.home / transaction["probeQuarantineName"]).exists()


def test_capability_recovery_rejects_replacement_and_malformed_quarantine(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    stage_name = ".qwen-capability-probe-33333333333333333333333333333333"
    final_name = ".qwen-capability-probe-published-44444444444444444444444444444444"
    stage = item.paths.home / stage_name
    stage.mkdir(mode=0o700)
    metadata = stage.stat()
    base = {
        "schemaVersion": core.SCHEMA_VERSION,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "probeParent": str(item.paths.home),
        "probeStageName": stage_name,
        "probeStageDev": metadata.st_dev,
        "probeStageIno": metadata.st_ino + 1,
        "probeFinalName": final_name,
        "probePublished": False,
    }
    item.store.write(base)

    with pytest.raises(RuntimeError, match="artifact changed"):
        item._recover_capability_probe(
            base,
            receipt_prefix="probe",
            expected_parent=item.paths.home,
        )
    assert stage.is_dir()

    malformed = dict(base)
    malformed["probeStageDev"] = metadata.st_dev
    malformed["probeStageIno"] = metadata.st_ino
    malformed["probeQuarantineName"] = "../unsafe"
    item.store.write(malformed)
    with pytest.raises(RuntimeError, match="quarantine receipt is malformed"):
        item._recover_capability_probe(
            malformed,
            receipt_prefix="probe",
            expected_parent=item.paths.home,
        )
    assert stage.is_dir()


def test_runtime_quiescence_guard_requires_durable_checkpoint_before_lock_creation(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)

    with pytest.raises(RuntimeError, match="requires a durable transaction checkpoint"):
        with item._runtime_quiescence_guard():
            pytest.fail("guard must not yield without a durable checkpoint")

    assert not (item.paths.project_root / "data/index.lock").exists()


def test_runtime_quiescence_checkpoints_lock_identity_before_any_lock_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    (item.paths.project_root / "data").mkdir(mode=0o700)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    events: list[str] = []
    real_fsync = core.os.fsync

    def record_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def checkpoint(receipt: dict[str, Any]) -> None:
        if "indexLockStageDev" in receipt:
            events.append("checkpoint:index-stage")
        if receipt.get("indexLockPublished") is True:
            events.append("checkpoint:index-published")
        if "snapshotLockStageDev" in receipt:
            events.append("checkpoint:snapshot-stage")
        if "snapshotLockDev" in receipt:
            events.append("checkpoint:snapshot-final")

    monkeypatch.setattr(core.os, "fsync", record_fsync)
    with item._runtime_quiescence_guard(checkpoint=checkpoint) as receipt:
        receipt["persisted"] = True

    assert events.index("checkpoint:index-stage") < events.index("fsync")
    assert events.index("fsync") < events.index("checkpoint:index-published")
    assert events.index("checkpoint:index-published") < events.index("checkpoint:snapshot-stage")
    assert events.index("checkpoint:snapshot-stage") < events.index("checkpoint:snapshot-final")
    assert not (item.paths.project_root / "data/index.lock").exists()


def test_prepare_project_root_checkpoints_exact_identity_before_bootstrap(tmp_path: Path) -> None:
    item = manager(tmp_path)
    item.paths.project_root.rmdir()
    transaction = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "fresh-project",
        "phase": "staging",
        "ownedAssets": [],
        "projectExisted": False,
        "projectCreatePlanned": True,
        "projectCreated": False,
    }
    item.store.write(transaction)

    item._prepare_project_root(transaction)

    metadata = item.paths.project_root.stat()
    persisted = item.store.read()
    assert transaction["projectCreated"] is True
    assert persisted["projectCreated"] is True
    assert (persisted["projectRootDev"], persisted["projectRootIno"]) == (
        metadata.st_dev, metadata.st_ino,
    )
    assert not any(item.paths.project_root.iterdir())


def test_managed_contract_requires_exact_alert_delivery_env_and_tools(tmp_path: Path) -> None:
    item = manager(tmp_path)
    spec = item._incremental_spec()
    expected = job_for_spec(spec, job_id="inc", enabled=True)
    assert core._job_matches_spec(expected, spec, require_enabled=True)

    cli_normalized = json.loads(json.dumps(expected))
    cli_normalized["delivery"] = {"mode": "none", "channel": "last"}
    assert core._job_matches_spec(cli_normalized, spec, require_enabled=True)

    for mutation in (
        lambda value: value["failureAlert"].pop("mode"),
        lambda value: value.update(description="drifted description"),
        lambda value: value.update(sessionTarget="main"),
        lambda value: value["failureAlert"].update(accountId="another-account"),
        lambda value: value.update(delivery={"mode": "announce"}),
        lambda value: value["payload"]["env"].update(EXTRA="unexpected"),
        lambda value: value["payload"].update(toolsAllow=["exec"]),
    ):
        changed = json.loads(json.dumps(expected))
        mutation(changed)
        assert not core._job_matches_spec(changed, spec, require_enabled=True)

    no_target = manager(tmp_path / "no-target", report_to=None)
    no_target_spec = no_target._incremental_spec()
    unexpected_target = job_for_spec(no_target_spec, job_id="extra-target", enabled=True)
    unexpected_target["failureAlert"]["to"] = "channel:unexpected"
    assert not core._job_matches_spec(unexpected_target, no_target_spec, require_enabled=True)


@pytest.mark.parametrize(
    ("delivery", "accepted"),
    [
        ({"mode": "none"}, True),
        ({"mode": "none", "channel": "last"}, True),
        ({}, False),
        ({"mode": "none", "channel": "discord"}, False),
        ({"mode": "none", "to": "channel:unexpected"}, False),
        ({"mode": "none", "accountId": "default"}, False),
        ({"mode": "none", "channel": "last", "to": "channel:unexpected"}, False),
        ({"mode": "none", "channel": "last", "unexpected": "field"}, False),
    ],
)
def test_no_delivery_contract_accepts_only_supported_cli_readback_shapes(
    delivery: dict[str, str], accepted: bool,
) -> None:
    assert core._no_delivery_contract(delivery) is accepted


def test_legacy_incremental_accepts_only_supported_no_delivery_normalization(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    job = job_for_spec(item._incremental_spec(), job_id="legacy-incremental", enabled=True)
    job["description"] = None
    job["payload"] = {
        "kind": "command",
        "argv": [str(item.paths.project_root / "scripts/knowledge_index_incremental.sh")],
        "cwd": str(item.paths.project_root),
        "timeoutSeconds": 7200,
        "noOutputTimeoutSeconds": 900,
        "outputMaxBytes": 65536,
    }
    job["delivery"] = {"mode": "none", "channel": "last"}
    job["failureAlert"] = None

    assert item._legacy_incremental_job(job)

    job["delivery"]["to"] = "channel:unexpected"
    assert not item._legacy_incremental_job(job)


def test_legacy_owned_command_tools_policy_fails_before_mutation(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    legacy = job_for_spec(item._incremental_spec(), job_id="legacy-incremental", enabled=True)
    legacy["description"] = None
    legacy["payload"] = {
        "kind": "command",
        "argv": [str(item.paths.project_root / "scripts/knowledge_index_incremental.sh")],
        "cwd": str(item.paths.project_root),
        "timeoutSeconds": 7200,
        "noOutputTimeoutSeconds": 900,
        "outputMaxBytes": 65536,
        "toolsAllow": [],
    }
    legacy["failureAlert"] = None
    cli.jobs = [legacy]

    with pytest.raises(RuntimeError, match="safe upgrade allowlist"):
        item._preflight_cron_inventory()

    assert cli.calls == [["cron", "list", "--all", "--json"]]


def test_operator_legacy_migration_requires_exact_id_and_fingerprint(tmp_path: Path) -> None:
    base = manager(tmp_path)
    candidate = legacy_snapshot_job(
        base, job_id="operator-selected", declaration_key="customer-owned-key",
    )

    missing_hash = manager(tmp_path, legacy_snapshot_job_id="operator-selected")
    with pytest.raises(RuntimeError, match="both job id and SHA-256"):
        missing_hash.preflight()

    mismatch = manager(
        tmp_path,
        legacy_snapshot_job_id="operator-selected",
        legacy_snapshot_job_sha256="0" * 64,
    )
    mismatch.cli.jobs = [candidate]
    with pytest.raises(RuntimeError, match="fingerprint"):
        mismatch._preflight_cron_inventory()


def test_sensitive_owned_cron_env_blocks_before_transaction_without_echoing_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = manager(tmp_path)
    unsafe = job_for_spec(item._incremental_spec(), job_id="unsafe", enabled=True)
    secret_value = "sk-" + "ThisMustNeverAppearInDiagnostics123456789"
    unsafe["payload"]["env"]["Auth_Token"] = secret_value
    item.cli.jobs = [unsafe]
    began = False

    def begin(**_kwargs: Any) -> dict[str, Any]:
        nonlocal began
        began = True
        return {}

    monkeypatch.setattr(item, "begin", begin)
    with pytest.raises(RuntimeError) as failure:
        item._integrate_locked({"runtimePort": 18888})

    assert began is False
    assert secret_value not in str(failure.value)


@pytest.mark.parametrize(
    "unsafe_field",
    [
        {"AUTH_TOKEN": "redacted-fixture-value"},
        {"note": "sk-" + "TokenShapedFixtureValue123456789"},
    ],
)
def test_transaction_receipt_rejects_case_insensitive_keys_and_token_shaped_values(
    tmp_path: Path, unsafe_field: dict[str, str]
) -> None:
    store = core.TransactionStore(tmp_path / "private-state")
    with pytest.raises(ValueError, match="forbidden sensitive field"):
        store.write({"schemaVersion": 1, "runId": "fixture", **unsafe_field})
    assert not store.manifest_path.exists()


def test_transaction_store_ignores_stale_interrupted_temporary_file(tmp_path: Path) -> None:
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    stale = state / "transaction.json.tmp"
    stale.write_text("partial", encoding="utf-8")
    stale.chmod(0o600)
    store = core.TransactionStore(state)

    store.write({"schemaVersion": 1, "phase": "checkpointed"})

    assert store.read()["phase"] == "checkpointed"
    assert stale.read_text(encoding="utf-8") == "partial"


def test_both_recurring_jobs_are_verified_disabled_before_global_enable(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    transaction = write_staging_transaction(item, [], set())

    incremental_id = item._apply_managed_spec(item._incremental_spec(), transaction)
    snapshot_id = item._apply_managed_spec(item._snapshot_spec(), transaction)

    assert all(job["enabled"] is False for job in cli.jobs)
    item._verify_recurring_specs(enabled=False)
    item._enable_recurring_jobs([incremental_id, snapshot_id])
    assert all(job["enabled"] is True for job in cli.jobs)
    assert cli.calls[0] and ["cron", "list", "--all", "--json"] in cli.calls


def test_cli_no_delivery_normalization_passes_disabled_and_enabled_readback(
    tmp_path: Path,
) -> None:
    class NormalizingCronCli(StatefulCronCli):
        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:4] == ["cron", "list", "--all", "--json"]:
                for job in self.jobs:
                    if job.get("delivery") == {"mode": "none"}:
                        job["delivery"] = {"mode": "none", "channel": "last"}
            return super().json(args, timeout=timeout)

    cli = NormalizingCronCli()
    item = manager(tmp_path, cli)
    transaction = write_staging_transaction(item, [], set())

    job_id = item._apply_managed_spec(
        item._incremental_spec(), transaction, enable=True,
    )

    installed = next(job for job in cli.jobs if job["id"] == job_id)
    assert installed["enabled"] is True
    assert installed["delivery"] == {"mode": "none", "channel": "last"}


def test_preflight_adopts_only_exact_known_legacy_or_operator_id(tmp_path: Path) -> None:
    item = manager(tmp_path)
    known = legacy_snapshot_job(item)
    item.cli.jobs = [known]
    jobs, legacy = item._preflight_cron_inventory()
    assert jobs == [known] and legacy == [known]

    selected_base = manager(tmp_path / "selected")
    unknown = legacy_snapshot_job(
        selected_base, job_id="operator-selected", declaration_key="customer-owned-key",
    )
    selected = manager(
        tmp_path / "selected",
        legacy_snapshot_job_id="operator-selected",
        legacy_snapshot_job_sha256=core._job_contract_hash(unknown),
    )
    selected.cli.jobs = [unknown]
    assert selected._preflight_cron_inventory()[1] == [unknown]


def test_unknown_job_targeting_owned_wrapper_blocks_without_mutation(tmp_path: Path) -> None:
    item = manager(tmp_path)
    wrapper = item.paths.project_root / "scripts/run_verified_snapshot.py"
    item.cli.jobs = [{
        "id": "unknown",
        "declarationKey": "customer-job",
        "payload": {"argv": [str(Path(sys.executable)), str(wrapper), "--ownership-manifest", "/tmp/x"]},
    }]

    with pytest.raises(RuntimeError, match="Unknown cron job"):
        item._preflight_cron_inventory()

    assert len(item.cli.jobs) == 1 and item.cli.jobs[0]["id"] == "unknown"


@pytest.mark.parametrize("owned", ["incremental", "snapshot"])
def test_unknown_job_with_extra_argv_or_shell_still_blocks_owned_wrapper(
    tmp_path: Path, owned: str
) -> None:
    item = manager(tmp_path)
    if owned == "incremental":
        script = item.paths.project_root / "scripts/knowledge_index_incremental.sh"
        argv = [str(script), "/unexpected/manifest.json"]
    else:
        script = item.paths.project_root / "scripts/run_verified_snapshot.py"
        argv = [str(Path(sys.executable)), str(script), "--unexpected", "/unexpected/manifest.json"]
    item.cli.jobs = [{
        "id": f"unknown-{owned}",
        "declarationKey": f"customer-{owned}",
        "payload": {"argv": argv},
    }]

    with pytest.raises(RuntimeError, match="Unknown cron job"):
        item._preflight_cron_inventory()

    assert item.cli.jobs[0]["declarationKey"] == f"customer-{owned}"


@pytest.mark.parametrize("declaration_key", [None, ""])
def test_exact_operator_approved_disabled_incremental_collision_is_preserved_as_unknown(
    tmp_path: Path, declaration_key: str | None,
) -> None:
    base = manager(tmp_path)
    collision = approved_disabled_incremental_collision_job(
        base, declaration_key=declaration_key,
    )
    before = core._job_contract_hash(collision, include_id=True)
    cli = StatefulCronCli()
    item = manager(
        tmp_path,
        cli,
        approved_disabled_collision=collision_approval(collision),
    )
    cli.jobs = [collision]

    jobs, legacy = item._preflight_cron_inventory()

    assert jobs == [collision]
    assert legacy == []
    assert core._job_contract_hash(cli.jobs[0], include_id=True) == before
    assert not any(call[:2] in (["cron", "rm"], ["cron", "edit"], ["cron", "disable"])
                   for call in cli.calls)
    assert item._ownership_payload()["approvedDisabledCollision"] == {
        "jobId": collision["id"],
        "contractSha256": before,
        "role": "incremental",
    }


def test_unapproved_exact_disabled_collision_fails_closed_with_safe_review_identity(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    collision = approved_disabled_incremental_collision_job(item)
    before = core._job_contract_hash(collision, include_id=True)
    item.cli.jobs = [collision]

    with pytest.raises(RuntimeError) as caught:
        item._preflight_cron_inventory()

    message = str(caught.value)
    assert collision["id"] in message
    assert before in message
    assert "incremental" in message
    assert "ID-inclusive SHA-256" in message
    assert core._job_contract_hash(item.cli.jobs[0], include_id=True) == before


def test_disabled_collision_approval_hash_binds_the_job_id(tmp_path: Path) -> None:
    item = manager(tmp_path)
    one = approved_disabled_incremental_collision_job(item, job_id="legacy-one")
    two = {**one, "id": "legacy-two"}

    assert core._job_contract_hash(one, include_id=True) != core._job_contract_hash(
        two, include_id=True,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"job_id": 1, "contract_sha256": "a" * 64, "role": "incremental"},
        {"job_id": "legacy", "contract_sha256": 1, "role": "incremental"},
        {"job_id": "legacy", "contract_sha256": "a" * 64, "role": 1},
    ],
)
def test_disabled_collision_approval_contract_rejects_non_string_fields(
    values: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        core.ApprovedDisabledCronCollision(**values)


@pytest.mark.parametrize(
    ("case", "mutation"),
    [
        ("enabled", lambda job, item: job.update(enabled=True)),
        ("declaration", lambda job, item: job.update(declarationKey="customer-owned")),
        ("extra-argv", lambda job, item: job["payload"]["argv"].append("--unexpected")),
        ("alternate-shell", lambda job, item: job["payload"]["argv"].__setitem__(0, "bash")),
        ("cwd", lambda job, item: job["payload"].update(cwd=str(item.paths.project_root))),
        ("env", lambda job, item: job["payload"].update(env={"SAFE": "fixture"})),
        ("tools", lambda job, item: job["payload"].update(toolsAllow=[])),
        ("limit", lambda job, item: job["payload"].update(timeoutSeconds=1801)),
        ("schedule", lambda job, item: job["schedule"].update(staggerMs=0)),
        ("delivery", lambda job, item: job["delivery"].update(accountId="default")),
        ("alert", lambda job, item: job.update(failureAlert={})),
        (
            "snapshot-role",
            lambda job, item: job["payload"].update(argv=[
                str(Path(sys.executable)),
                str(item.paths.project_root / "scripts/run_verified_snapshot.py"),
                "--ownership-manifest", str(item.ownership_manifest),
            ]),
        ),
    ],
)
def test_recomputed_hash_cannot_approve_an_unsafe_disabled_collision_contract(
    tmp_path: Path, case: str, mutation: Any,
) -> None:
    case_root = tmp_path / case
    base = manager(case_root)
    collision = approved_disabled_incremental_collision_job(base)
    mutation(collision, base)
    cli = StatefulCronCli()
    item = manager(
        case_root,
        cli,
        approved_disabled_collision=collision_approval(collision),
    )
    cli.jobs = [collision]

    with pytest.raises(RuntimeError, match="approval|collision|contract"):
        item._preflight_cron_inventory()

    assert len(cli.calls) == 1


@pytest.mark.parametrize("fault", ["wrong-id", "wrong-hash", "wrong-role", "missing-job"])
def test_disabled_collision_approval_requires_exact_id_hash_role_and_presence(
    tmp_path: Path, fault: str,
) -> None:
    case_root = tmp_path / fault
    base = manager(case_root)
    collision = approved_disabled_incremental_collision_job(base)
    approval = collision_approval(collision)
    if fault == "wrong-id":
        approval = core.ApprovedDisabledCronCollision(
            job_id="different-id", contract_sha256=approval.contract_sha256, role="incremental",
        )
    elif fault == "wrong-hash":
        approval = core.ApprovedDisabledCronCollision(
            job_id=approval.job_id, contract_sha256="0" * 64, role="incremental",
        )
    elif fault == "wrong-role":
        with pytest.raises(ValueError, match="incremental"):
            core.ApprovedDisabledCronCollision(
                job_id=approval.job_id,
                contract_sha256=approval.contract_sha256,
                role="snapshot",
            )
        return
    cli = StatefulCronCli()
    item = manager(case_root, cli, approved_disabled_collision=approval)
    cli.jobs = [] if fault == "missing-job" else [collision]

    with pytest.raises(RuntimeError, match="approval|fingerprint|exactly once"):
        item._preflight_cron_inventory()


def test_duplicate_approved_id_and_second_wrapper_collision_both_fail_closed(
    tmp_path: Path,
) -> None:
    base = manager(tmp_path)
    approved = approved_disabled_incremental_collision_job(base)
    approval = collision_approval(approved)

    duplicate = manager(
        tmp_path / "duplicate",
        approved_disabled_collision=approval,
    )
    duplicate.cli.jobs = [approved, json.loads(json.dumps(approved))]
    with pytest.raises(RuntimeError, match="duplicate"):
        duplicate._preflight_cron_inventory()

    second_root = tmp_path / "second"
    second_base = manager(second_root)
    first = approved_disabled_incremental_collision_job(second_base, job_id="first")
    second = approved_disabled_incremental_collision_job(second_base, job_id="second")
    with_second = manager(
        second_root,
        approved_disabled_collision=collision_approval(first),
    )
    with_second.cli.jobs = [first, second]
    with pytest.raises(RuntimeError, match="requires explicit approval"):
        with_second._preflight_cron_inventory()


def prepare_collision_integration_runtime(
    item: core.IntegrationManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
) -> None:
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    base = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": run_id,
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "healthReceiptExisted": False,
        "projectExisted": True,
    }

    @contextmanager
    def guard(*, checkpoint=None) -> Iterator[dict[str, Any]]:
        yield {"snapshotLockCreated": False, "persisted": False}

    monkeypatch.setattr(item, "begin", lambda **_: dict(base))
    monkeypatch.setattr(item, "_prepare_snapshot_root", lambda _transaction: create_snapshot_root(item))
    monkeypatch.setattr(item, "_runtime_quiescence_guard", guard)
    monkeypatch.setattr(item, "bootstrap_project", lambda _: False)
    monkeypatch.setattr(item, "synchronize_project_runtime", lambda *_: None)
    monkeypatch.setattr(item, "_allowed_projects", lambda: [])
    monkeypatch.setattr(item, "configure_openclaw", lambda _allowed, **_: None)
    monkeypatch.setattr(item, "install_launchd_plist", lambda _: None)
    monkeypatch.setattr(item, "activate_launchd", lambda: None)
    monkeypatch.setattr(item, "mark_ready_or_schedule_build", lambda *_: ("READY", None))
    monkeypatch.setattr(item, "_sha256_config", lambda _: "0" * 64)
    monkeypatch.setattr(item, "_verify_local_source_map", lambda: None)
    monkeypatch.setattr(item, "_verify_runtime_contract_files", lambda: None)
    monkeypatch.setattr(item, "_verify_snapshot_wrapper_contract", lambda: None)
    monkeypatch.setattr(item, "_verify_plugin_skill_gateway", lambda: (True, True, True))
    monkeypatch.setattr(item, "_health_receipt_status", lambda: "ok")

    def write_health(**_: Any) -> None:
        item.health_receipt_path.parent.mkdir(parents=True, exist_ok=True)
        item.health_receipt_path.write_text("{}", encoding="utf-8")
        item.health_receipt_path.chmod(0o600)

    monkeypatch.setattr(item, "_write_health_receipt", write_health)


@pytest.mark.parametrize(
    ("prior_contract", "expected_action"),
    [(None, "committed"), (1, "upgraded")],
)
def test_approved_disabled_collision_survives_fresh_upgrade_and_idempotent_transactions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_contract: int | None,
    expected_action: str,
) -> None:
    case_root = tmp_path / expected_action
    base = manager(case_root)
    collision = approved_disabled_incremental_collision_job(base)
    approved_hash = core._job_contract_hash(collision, include_id=True)
    collision_before = json.loads(json.dumps(collision))
    cli = StatefulCronCli()
    item = manager(
        case_root,
        cli,
        approved_disabled_collision=collision_approval(collision),
    )
    cli.jobs = [collision]
    if prior_contract is not None:
        item.store.write({
            "schemaVersion": 1,
            "contractVersion": prior_contract,
            "runId": "prior-install",
            "phase": "committed",
            "ownership": {"schema": "qwen-local-openclaw.v1"},
        })
    prepare_collision_integration_runtime(item, monkeypatch, run_id=expected_action)

    result = item._integrate_locked({"runtimePort": 18888})

    assert result["transaction"] == expected_action
    transaction = item.store.read()
    assert transaction["phase"] == "committed"
    assert transaction["ownership"]["approvedDisabledCollision"] == {
        "jobId": collision["id"],
        "contractSha256": approved_hash,
        "role": "incremental",
    }
    assert transaction["cronUnknownHashesBefore"] == {collision["id"]: approved_hash}
    assert collision["id"] not in transaction["cronTargetIdsBefore"]
    assert all(definition.get("id") != collision["id"]
               for definition in transaction["cronDefinitionsBefore"])
    preserved = next(job for job in cli.jobs if job["id"] == collision["id"])
    assert preserved == collision_before
    assert core._job_contract_hash(preserved, include_id=True) == approved_hash
    assert not any(
        call[:2] in (["cron", "rm"], ["cron", "edit"], ["cron", "disable"])
        and len(call) > 2 and call[2] == collision["id"]
        for call in cli.calls
    )

    mutation_count = sum(
        call[:2] in (["cron", "add"], ["cron", "rm"], ["cron", "edit"], ["cron", "disable"])
        for call in cli.calls
    )
    again = item._integrate_locked({"runtimePort": 18888})
    assert again["transaction"] == "already_current"
    assert mutation_count == sum(
        call[:2] in (["cron", "add"], ["cron", "rm"], ["cron", "edit"], ["cron", "disable"])
        for call in cli.calls
    )
    preserved_again = next(job for job in cli.jobs if job["id"] == collision["id"])
    assert preserved_again == collision_before
    assert core._job_contract_hash(preserved_again, include_id=True) == approved_hash


def test_approved_disabled_collision_fault_rollback_preserves_exact_unknown_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = manager(tmp_path)
    collision = approved_disabled_incremental_collision_job(base)
    approved_hash = core._job_contract_hash(collision, include_id=True)
    collision_before = json.loads(json.dumps(collision))
    cli = StatefulCronCli()
    item = manager(
        tmp_path,
        cli,
        approved_disabled_collision=collision_approval(collision),
    )
    cli.jobs = [collision]
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    base_transaction = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "approved-collision-fault",
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "configBackupPath": str(item.paths.state_root / "fixture-backup"),
        "preConfigSha256": "1" * 64,
        "snapshotRunMarkerSha256": "2" * 64,
        "snapshotRunDev": 1,
        "snapshotRunIno": 2,
        "projectExisted": True,
        "healthReceiptExisted": False,
    }
    monkeypatch.setattr(item, "begin", lambda **_: dict(base_transaction))
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)
    real_quiesce = item._quiesce_prior_jobs

    def fail_after_quiescence(
        jobs: list[dict[str, Any]], target_ids: set[str], hashes: dict[str, str],
    ) -> list[str]:
        real_quiesce(jobs, target_ids, hashes)
        raise RuntimeError("fixture fault after cron preservation check")

    monkeypatch.setattr(item, "_quiesce_prior_jobs", fail_after_quiescence)

    with pytest.raises(RuntimeError, match="fixture fault"):
        item._integrate_locked({"runtimePort": 18888})

    transaction = item.store.read()
    assert transaction["phase"] == "rolled_back"
    assert transaction["cronUnknownHashesBefore"] == {collision["id"]: approved_hash}
    assert transaction["cronTargetIdsBefore"] == []
    assert cli.jobs == [collision_before]
    assert core._job_contract_hash(cli.jobs[0], include_id=True) == approved_hash
    assert not any(
        call[:2] in (["cron", "rm"], ["cron", "edit"], ["cron", "disable"])
        and len(call) > 2 and call[2] == collision["id"]
        for call in cli.calls
    )


def test_approved_disabled_collision_hash_drift_blocks_before_any_cron_mutation(
    tmp_path: Path,
) -> None:
    base = manager(tmp_path)
    collision = approved_disabled_incremental_collision_job(base)
    cli = StatefulCronCli()
    item = manager(
        tmp_path,
        cli,
        approved_disabled_collision=collision_approval(collision),
    )
    cli.jobs = [collision]
    jobs, _ = item._preflight_cron_inventory()
    hashes = item._inventory_hashes(jobs)
    collision["schedule"]["expr"] = "31 6 * * *"

    with pytest.raises(RuntimeError, match="changed between preflight and quiescence"):
        item._quiesce_prior_jobs(jobs, set(), hashes)

    assert not any(
        call[:2] in (["cron", "rm"], ["cron", "edit"], ["cron", "disable"])
        for call in cli.calls
    )


def test_approved_disabled_collision_receipt_tamper_fails_closed(tmp_path: Path) -> None:
    base = manager(tmp_path)
    collision = approved_disabled_incremental_collision_job(base)
    item = manager(
        tmp_path,
        approved_disabled_collision=collision_approval(collision),
    )
    transaction = {
        "ownership": item._ownership_payload(),
        "cronUnknownHashesBefore": {collision["id"]: "0" * 64},
    }

    with pytest.raises(RuntimeError, match="unknown-inventory receipt drifted"):
        item._verify_approved_collision_receipt(transaction, [collision])


def test_activation_failure_invokes_rollback_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = manager(tmp_path)
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    events: list[str] = []
    base = {
        "schemaVersion": 1, "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "fault", "phase": "prepared",
        "ownedAssets": [], "configPath": str(config), "healthReceiptExisted": False,
        "projectExisted": True,
    }

    @contextmanager
    def guard(*, checkpoint=None) -> Iterator[dict[str, Any]]:
        yield {"snapshotLockCreated": False, "persisted": False}

    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "begin", lambda **_: dict(base))
    monkeypatch.setattr(item, "_prepare_snapshot_root", lambda _transaction: create_snapshot_root(item))
    monkeypatch.setattr(item, "_runtime_quiescence_guard", guard)
    monkeypatch.setattr(item, "bootstrap_project", lambda _: False)
    monkeypatch.setattr(item, "synchronize_project_runtime", lambda *_: events.append("sync"))
    monkeypatch.setattr(item, "_allowed_projects", lambda: [])
    monkeypatch.setattr(item, "configure_openclaw", lambda _allowed, **_: events.append("configure"))
    monkeypatch.setattr(item, "install_launchd_plist", lambda _: events.append("plist"))
    monkeypatch.setattr(item, "activate_launchd", lambda: events.append("launchd"))
    staged = iter(["incremental", "snapshot"])
    monkeypatch.setattr(item, "_apply_managed_spec", lambda *_: next(staged))
    monkeypatch.setattr(item, "_verify_recurring_specs", lambda **_: events.append("global-disabled"))
    monkeypatch.setattr(item, "disable_owned_gemini_jobs", lambda: [])
    monkeypatch.setattr(item, "mark_ready_or_schedule_build", lambda *_: ("READY", None))
    monkeypatch.setattr(item, "_verify_success_cron_inventory", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(item, "_sha256_config", lambda _: "0" * 64)
    monkeypatch.setattr(item.cli, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(item, "_enable_recurring_jobs", lambda _: (_ for _ in ()).throw(RuntimeError("enable fault")))
    monkeypatch.setattr(item, "_rollback_locked", lambda **_: events.append("rollback"))

    with pytest.raises(RuntimeError, match="enable fault"):
        item._integrate_locked({"runtimePort": 18888})

    assert events.index("global-disabled") < events.index("rollback")
    assert item.store.read()["phase"] == "failed"


def test_failed_phase_write_failure_cannot_suppress_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = manager(tmp_path)
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    base = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "failed-write",
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "projectExisted": True,
        "healthReceiptExisted": False,
    }
    rolled_back: list[bool] = []
    real_write = item.store.write

    def fail_only_failed_phase(payload: dict[str, Any]) -> Path:
        if payload.get("phase") == "failed":
            raise OSError("simulated status write failure")
        return real_write(payload)

    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "begin", lambda **_: dict(base))
    monkeypatch.setattr(
        item,
        "_quiesce_prior_jobs",
        lambda *_: (_ for _ in ()).throw(RuntimeError("primary failure")),
    )
    monkeypatch.setattr(item.store, "write", fail_only_failed_phase)
    monkeypatch.setattr(item, "_rollback_locked", lambda **_: rolled_back.append(True) or {"ok": True})

    with pytest.raises(RuntimeError, match="primary failure"):
        item._integrate_locked({"runtimePort": 18888})

    assert rolled_back == [True]


def test_incomplete_automatic_rollback_raises_typed_recovery_state_and_preserves_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    primary = ValueError("primary integration fault")
    rollback = OSError("rollback verification fault")
    base = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "rollback-incomplete",
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "projectExisted": True,
        "healthReceiptExisted": False,
    }
    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "begin", lambda **_: dict(base))
    monkeypatch.setattr(item, "_quiesce_prior_jobs", lambda *_: (_ for _ in ()).throw(primary))
    monkeypatch.setattr(item, "_rollback_locked", lambda **_: (_ for _ in ()).throw(rollback))

    with pytest.raises(core.IntegrationRollbackIncomplete) as caught:
        item._integrate_locked({"runtimePort": 18888})

    assert caught.value.original_error is primary
    assert caught.value.rollback_error is rollback
    assert caught.value.__cause__ is primary
    assert item.store.read()["phase"] == "rollback_failed"


def customer_job(*, job_id: str = "customer") -> dict[str, Any]:
    return {
        "id": job_id,
        "name": "Customer-owned job",
        "description": "Must remain byte-for-byte equivalent.",
        "enabled": True,
        "declarationKey": "customer-owned-v1",
        "sessionTarget": "isolated",
        "sessionKey": None,
        "agentId": None,
        "deleteAfterRun": False,
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Taipei", "staggerMs": 0},
        "payload": {
            "kind": "command",
            "argv": ["/usr/bin/true"],
            "cwd": "/tmp",
            "timeoutSeconds": 30,
            "noOutputTimeoutSeconds": 30,
            "outputMaxBytes": 1024,
        },
        "delivery": {"mode": "none"},
        "failureAlert": None,
    }


def write_staging_transaction(
    item: core.IntegrationManager,
    jobs_before: list[dict[str, Any]],
    target_ids: set[str],
) -> dict[str, Any]:
    definitions = [
        core._job_definition(job) for job in jobs_before
        if str(job["id"]) in target_ids
    ]
    inventory_hashes = item._inventory_hashes(jobs_before)
    transaction = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "staging-fixture",
        "phase": "replacing_managed_cron",
        "cronContractHashVersion": core.CRON_CONTRACT_HASH_VERSION,
        "cronDefinitionsBefore": definitions,
        "cronInventoryTotalBefore": len(jobs_before),
        "cronInventoryHashesBefore": inventory_hashes,
        "cronUnknownHashesBefore": {
            job_id: fingerprint for job_id, fingerprint in inventory_hashes.items()
            if job_id not in target_ids
        },
        "cronReplaceIdsBefore": sorted(
            str(job["id"]) for job in jobs_before
            if job.get("declarationKey") in core.MANAGED_CRON_KEYS
        ),
        "disabledGeminiJobs": [
            {"id": str(job["id"]), "wasEnabled": True}
            for job in jobs_before
            if str(job["id"]) in target_ids
            and job.get("declarationKey") == core.GEMINI_DECLARATION_KEY
            and job.get("enabled") is True
        ],
        "cronPreservedGeminiHashesAfterQuiesce": {
            str(job["id"]): core._job_contract_hash(
                {**core._job_definition(job), "enabled": False},
                include_id=True,
            )
            for job in jobs_before
            if str(job["id"]) in target_ids
            and job.get("declarationKey") == core.GEMINI_DECLARATION_KEY
        },
        "cronTargetIdsBefore": sorted(target_ids),
        "managedCronIdsAfter": [],
        "cronStagingIntents": {},
        "cronRestoreIntents": {},
        "restoredCronIdsByDeclaration": {},
    }
    item.store.write(transaction)
    return transaction


def test_replacement_receipt_graph_cross_binds_inventory_managed_and_gemini(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    gemini = gemini_job(item, job_id="prior-gemini", enabled=True)
    unknown = customer_job()
    transaction = write_staging_transaction(
        item,
        [managed, gemini, unknown],
        {"prior-managed", "prior-gemini"},
    )

    assert item._validate_replacement_receipt_graph(transaction) is True
    assert transaction["cronInventoryTotalBefore"] == 3
    assert transaction["cronReplaceIdsBefore"] == ["prior-managed"]
    assert transaction["disabledGeminiJobs"] == [
        {"id": "prior-gemini", "wasEnabled": True}
    ]


@pytest.mark.parametrize(
    "fault",
    ["missing", "extra", "nonhex", "wrong-hash-with-live-drift"],
)
def test_replacement_receipt_graph_rejects_gemini_rebaseline(
    tmp_path: Path, fault: str,
) -> None:
    item = manager(tmp_path / fault)
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    gemini = gemini_job(item, job_id="prior-gemini", enabled=True)
    transaction = write_staging_transaction(
        item,
        [managed, gemini],
        {"prior-managed", "prior-gemini"},
    )
    receipt = transaction["cronPreservedGeminiHashesAfterQuiesce"]
    if fault == "missing":
        transaction.pop("cronPreservedGeminiHashesAfterQuiesce")
    elif fault == "extra":
        receipt["extra-gemini"] = "0" * 64
    elif fault == "nonhex":
        receipt["prior-gemini"] = "not-a-sha256"
    else:
        drifted = json.loads(json.dumps(gemini))
        drifted["enabled"] = False
        drifted["schedule"]["expr"] = "17 4 * * *"
        receipt["prior-gemini"] = core._job_contract_hash(
            drifted, include_id=True,
        )

    with pytest.raises(RuntimeError, match="Cron replacement"):
        item._validate_replacement_receipt_graph(transaction)


@pytest.mark.parametrize(
    "fault",
    ["inventory-total", "replace-ids", "removed-ids", "disabled-gemini", "phase"],
)
def test_replacement_receipt_graph_tamper_fails_closed(
    tmp_path: Path, fault: str,
) -> None:
    item = manager(tmp_path / fault)
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    gemini = gemini_job(item, job_id="prior-gemini", enabled=True)
    unknown = customer_job()
    transaction = write_staging_transaction(
        item,
        [managed, gemini, unknown],
        {"prior-managed", "prior-gemini"},
    )
    if fault == "inventory-total":
        transaction["cronInventoryTotalBefore"] += 1
    elif fault == "replace-ids":
        transaction["cronReplaceIdsBefore"] = []
    elif fault == "removed-ids":
        transaction["phase"] = "staging_managed_cron"
        transaction["removedManagedCronIdsBeforeAdd"] = []
    elif fault == "disabled-gemini":
        transaction["disabledGeminiJobs"] = []
    else:
        transaction["phase"] = "failed"
        transaction["failurePhase"] = "quiesced"

    with pytest.raises(RuntimeError, match="Cron replacement"):
        item._validate_replacement_receipt_graph(transaction)


@pytest.mark.parametrize(
    ("phase", "failure_phase"),
    [
        ("failed", None),
        ("failed", "unknown-phase"),
        ("rollback_failed", None),
        ("rollback_failed", "unknown-phase"),
    ],
)
def test_replacement_receipt_terminal_phase_requires_recognized_origin(
    tmp_path: Path, phase: str, failure_phase: str | None,
) -> None:
    item = manager(tmp_path / f"{phase}-{failure_phase}")
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    transaction = write_staging_transaction(
        item, [managed], {"prior-managed"},
    )
    transaction["phase"] = phase
    if failure_phase is not None:
        transaction["failurePhase"] = failure_phase

    with pytest.raises(RuntimeError, match="terminal phase authority"):
        item._validate_replacement_receipt_graph(transaction)


@pytest.mark.parametrize(
    "missing_field",
    ["cronReplaceIdsBefore", "removedManagedCronIdsBeforeAdd"],
)
def test_replacement_receipt_terminal_post_replace_requires_both_receipts(
    tmp_path: Path, missing_field: str,
) -> None:
    item = manager(tmp_path / missing_field)
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    transaction = write_staging_transaction(
        item, [managed], {"prior-managed"},
    )
    transaction.update({
        "phase": "failed",
        "failurePhase": "activation_pending",
        "removedManagedCronIdsBeforeAdd": ["prior-managed"],
    })
    transaction.pop(missing_field)

    with pytest.raises(RuntimeError, match="Cron replacement"):
        item._validate_replacement_receipt_graph(transaction)


def test_replacing_phase_rejects_premature_removed_receipt(tmp_path: Path) -> None:
    item = manager(tmp_path)
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    transaction = write_staging_transaction(
        item, [managed], {"prior-managed"},
    )
    transaction["removedManagedCronIdsBeforeAdd"] = ["prior-managed"]

    with pytest.raises(RuntimeError, match="phase boundary"):
        item._validate_replacement_receipt_graph(transaction)


@pytest.mark.parametrize("terminal", [False, True])
def test_new_only_phase_cannot_downgrade_by_removing_all_graph_fields(
    tmp_path: Path, terminal: bool,
) -> None:
    item = manager(tmp_path / str(terminal))
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    transaction = write_staging_transaction(
        item, [managed], {"prior-managed"},
    )
    transaction["phase"] = "activation_pending"
    transaction["removedManagedCronIdsBeforeAdd"] = ["prior-managed"]
    if terminal:
        transaction["failurePhase"] = transaction["phase"]
        transaction["phase"] = "failed"
    for field in (
        "cronInventoryTotalBefore", "disabledGeminiJobs",
        "cronReplaceIdsBefore", "removedManagedCronIdsBeforeAdd",
    ):
        transaction.pop(field)

    with pytest.raises(RuntimeError, match="receipt graph is missing"):
        item._validate_replacement_receipt_graph(transaction)


@pytest.mark.parametrize("origin", [None, "unknown-phase"])
def test_rolled_back_graph_requires_explicit_recognized_origin(
    tmp_path: Path, origin: str | None,
) -> None:
    item = manager(tmp_path / str(origin))
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    transaction = write_staging_transaction(
        item, [managed], {"prior-managed"},
    )
    transaction["phase"] = "rolled_back"
    if origin is not None:
        transaction["rollbackOriginPhase"] = origin

    with pytest.raises(RuntimeError, match="terminal phase authority"):
        item._validate_replacement_receipt_graph(transaction)


def test_rolled_back_graph_preserves_explicit_replacement_origin(tmp_path: Path) -> None:
    item = manager(tmp_path)
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    transaction = write_staging_transaction(
        item, [managed], {"prior-managed"},
    )
    transaction.update({
        "phase": "rolled_back",
        "rollbackOriginPhase": "replacing_managed_cron",
    })

    assert item._validate_replacement_receipt_graph(transaction) is True


@pytest.mark.parametrize(
    "fault",
    ["definition-hash", "target-partition", "unknown-collision", "unknown-gap"],
)
def test_replacement_graph_tamper_stops_before_any_exact_removal(
    tmp_path: Path, fault: str,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / fault, cli)
    managed = job_for_spec(
        item._incremental_spec(), job_id="prior-managed", enabled=True,
    )
    unknown = customer_job()
    jobs_before = [managed, unknown]
    cli.jobs = json.loads(json.dumps(jobs_before))
    hashes = item._inventory_hashes(jobs_before)
    transaction = write_staging_transaction(
        item, jobs_before, {"prior-managed"},
    )
    if fault == "definition-hash":
        transaction["cronDefinitionsBefore"][0]["name"] = "Different valid name"
    elif fault == "target-partition":
        transaction["cronTargetIdsBefore"] = []
    elif fault == "unknown-collision":
        transaction["cronUnknownHashesBefore"]["prior-managed"] = hashes[
            "prior-managed"
        ]
    else:
        transaction["cronUnknownHashesBefore"].pop("customer")
    item.store.write(transaction)

    with pytest.raises(RuntimeError, match="Cron replacement"):
        item._remove_prior_managed_jobs_for_replacement(
            jobs_before, {"prior-managed"}, hashes,
        )

    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)
    assert {job["id"] for job in cli.jobs} == {"prior-managed", "customer"}


def write_rollback_transaction(
    item: core.IntegrationManager,
    *,
    prior_definitions: list[dict[str, Any]],
    unknown: dict[str, Any],
    target_ids: list[str],
    managed_after: list[str],
) -> None:
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "runId": "rollback-fixture",
        "phase": "failed",
        "ownedAssets": [],
        "configPath": str(config),
        "configBackupPath": str(item.paths.state_root / "snapshots/run-00000000-0000-0000-0000-000000000001/openclaw-config.preinstall"),
        "preConfigSha256": "1" * 64,
        "snapshotRunMarkerSha256": "2" * 64,
        "snapshotRunDev": 1,
        "snapshotRunIno": 2,
        "cronMutationStarted": True,
        "runtimeMutationStarted": False,
        "cronContractHashVersion": core.CRON_CONTRACT_HASH_VERSION,
        "cronDefinitionsBefore": prior_definitions,
        "cronUnknownHashesBefore": {
            str(unknown["id"]): core._job_contract_hash(unknown, include_id=True),
        },
        "cronInventoryHashesBefore": {
            str(definition["id"]): core._job_contract_hash(definition, include_id=True)
            for definition in prior_definitions
        } | {
            str(unknown["id"]): core._job_contract_hash(unknown, include_id=True),
        },
        "cronPreservedGeminiHashesAfterQuiesce": {
            str(definition["id"]): core._job_contract_hash(
                {**definition, "enabled": False}, include_id=True,
            )
            for definition in prior_definitions
            if definition.get("declarationKey") == core.GEMINI_DECLARATION_KEY
        },
        "cronTargetIdsBefore": target_ids,
        "managedCronIdsAfter": managed_after,
        "cronStagingIntents": {},
        "cronRestoreIntents": {},
        "restoredCronIdsByDeclaration": {},
        "snapshotRootCreated": False,
        "snapshotLockCreated": False,
    })


def arm_activation_fail_safe_transaction(
    item: core.IntegrationManager,
    cli: StatefulCronCli,
    *,
    enabled_keys: set[str],
    include_initial: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    unknown = customer_job()
    write_rollback_transaction(
        item,
        prior_definitions=[],
        unknown=unknown,
        target_ids=[],
        managed_after=[],
    )
    transaction = item.store.read()
    jobs: list[dict[str, Any]] = []
    expected_by_key = {
        core.CRON_DECLARATION_KEY: "new-incremental",
        core.SNAPSHOT_CRON_DECLARATION_KEY: "new-snapshot",
    }
    for spec in (item._incremental_spec(), item._snapshot_spec()):
        intent = item._ensure_cron_intent(
            transaction,
            bucket_name="cronStagingIntents",
            declaration_key=spec.key,
            canonical_description=spec.description,
            role="managed",
            expected_factory=lambda description, expected=spec: (
                item._managed_pre_alert_definition(expected, description)
            ),
        )
        intent["jobId"] = expected_by_key[spec.key]
        intent["configured"] = True
        lifecycle = item._managed_intent_lifecycle_contracts(intent)
        job = json.loads(json.dumps(
            lifecycle[3 if spec.key in enabled_keys else 2]
        ))
        job["id"] = expected_by_key[spec.key]
        jobs.append(job)

    if include_initial:
        scheduled_at = "2026-09-07T07:00:00.000Z"
        intent = item._ensure_cron_intent(
            transaction,
            bucket_name="cronStagingIntents",
            declaration_key=core.INITIAL_CRON_DECLARATION_KEY,
            canonical_description=core.INITIAL_CRON_DESCRIPTION,
            role="initial",
            expected_factory=lambda description: item._initial_pre_alert_definition(
                description, scheduled_at,
            ),
            extra_fields={"scheduledAt": scheduled_at},
        )
        intent["jobId"] = "new-initial"
        intent["configured"] = True
        lifecycle = item._managed_intent_lifecycle_contracts(intent)
        job = json.loads(json.dumps(
            lifecycle[
                3 if core.INITIAL_CRON_DECLARATION_KEY in enabled_keys else 2
            ]
        ))
        job["id"] = "new-initial"
        jobs.append(job)
        expected_by_key[core.INITIAL_CRON_DECLARATION_KEY] = "new-initial"

    transaction.update({
        "phase": "failed",
        "failurePhase": "activation_pending",
        "cronInventoryTotalBefore": 1,
        "cronReplaceIdsBefore": [],
        "removedManagedCronIdsBeforeAdd": [],
        "disabledGeminiJobs": [],
        "cronId": expected_by_key[core.CRON_DECLARATION_KEY],
        "snapshotCronId": expected_by_key[core.SNAPSHOT_CRON_DECLARATION_KEY],
        "initialIndexJobId": expected_by_key.get(
            core.INITIAL_CRON_DECLARATION_KEY
        ),
        "indexState": "INDEX_BUILDING" if include_initial else "READY",
        "managedCronIdsAfter": list(expected_by_key.values()),
        "activationFailSafeRequired": True,
        "activationFailSafeDisabledCronIds": [],
        "activationFailSafeComplete": False,
    })
    item.store.write(transaction)
    cli.jobs = [*jobs, json.loads(json.dumps(unknown))]
    return transaction, unknown


@pytest.mark.parametrize(
    ("phase", "failure_phase"),
    [
        ("activation_pending", None),
        ("commit_closeout_pending", None),
        ("failed", "activation_pending"),
        ("rollback_failed", "commit_closeout_pending"),
    ],
)
def test_armed_noncommitted_reentry_disables_partial_activation_before_refusal(
    tmp_path: Path, phase: str, failure_phase: str | None,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / phase, cli)
    transaction, original_unknown = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={core.CRON_DECLARATION_KEY},
    )
    transaction["phase"] = phase
    if failure_phase is None:
        transaction.pop("failurePhase", None)
    else:
        transaction["failurePhase"] = failure_phase
    item.store.write(transaction)
    unknown = next(job for job in cli.jobs if job["id"] == "customer")
    unknown["description"] = "Concurrent customer change during installer crash."
    drifted_unknown = json.loads(json.dumps(unknown))

    with pytest.raises(RuntimeError, match="safely disabled and requires rollback"):
        item._integrate_locked({"runtimePort": 18888})

    assert all(job["enabled"] is False for job in cli.jobs if job["id"] != "customer")
    assert next(job for job in cli.jobs if job["id"] == "customer") == drifted_unknown
    assert original_unknown != drifted_unknown
    assert not any(call[:2] in (["cron", "add"], ["cron", "rm"]) for call in cli.calls)
    assert not any(
        call[:2] == ["cron", "edit"] and call[2] == "customer"
        for call in cli.calls
    )
    receipt = item.store.read()
    assert receipt["phase"] == phase
    assert receipt["activationFailSafeRequired"] is True
    assert receipt["activationFailSafeComplete"] is True
    assert set(receipt["activationFailSafeDisabledCronIds"]) == {
        "new-incremental", "new-snapshot",
    }


def test_armed_reentry_compensation_failure_is_typed_and_persisted(
    tmp_path: Path,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    transaction, unknown = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={core.CRON_DECLARATION_KEY, core.SNAPSHOT_CRON_DECLARATION_KEY},
    )
    transaction["phase"] = "activation_pending"
    transaction.pop("failurePhase", None)
    item.store.write(transaction)
    cli.jobs = [job for job in cli.jobs if job["id"] != "new-incremental"]

    with pytest.raises(core.ActivationFailSafeIncomplete) as caught:
        item._integrate_locked({"runtimePort": 18888})

    assert "compensation was incomplete" in str(caught.value.compensation_error)
    assert caught.value.recovery_state == "activation_fail_safe_incomplete"
    assert next(job for job in cli.jobs if job["id"] == "new-snapshot")[
        "enabled"
    ] is False
    assert next(job for job in cli.jobs if job["id"] == "customer") == unknown
    receipt = item.store.read()
    assert receipt["phase"] == "activation_pending"
    assert receipt["activationFailSafeRequired"] is True
    assert receipt["activationFailSafeStarted"] is True
    assert receipt["activationFailSafeComplete"] is False
    assert "new-snapshot" in receipt["activationFailSafeDisabledCronIds"]
    assert not any(call[:2] in (["cron", "add"], ["cron", "rm"]) for call in cli.calls)


def test_armed_reentry_rejects_malformed_marker_before_cron_mutation(
    tmp_path: Path,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    transaction, _ = arm_activation_fail_safe_transaction(
        item, cli, enabled_keys={core.CRON_DECLARATION_KEY},
    )
    transaction["phase"] = "activation_pending"
    transaction.pop("failurePhase", None)
    transaction["activationFailSafeRequired"] = "true"
    item.store.write(transaction)
    cli.calls.clear()

    with pytest.raises(RuntimeError, match="durable marker is malformed"):
        item._integrate_locked({"runtimePort": 18888})

    assert cli.calls == []
    assert next(job for job in cli.jobs if job["id"] == "new-incremental")[
        "enabled"
    ] is True


def test_activation_fail_safe_marker_write_failure_still_disables_every_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    _, unknown = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={core.CRON_DECLARATION_KEY, core.SNAPSHOT_CRON_DECLARATION_KEY},
    )
    real_write = item.store.write
    failed = False

    def fail_first_progress_write(payload: dict[str, Any]) -> Path:
        nonlocal failed
        if payload.get("activationFailSafeStarted") is True and not failed:
            failed = True
            raise OSError("simulated first progress write failure")
        return real_write(payload)

    monkeypatch.setattr(item.store, "write", fail_first_progress_write)

    with pytest.raises(RuntimeError, match="compensation was incomplete"):
        item._disable_uncommitted_managed_jobs_for_activation_failure(
            item.store.read()
        )

    assert failed is True
    assert all(job["enabled"] is False for job in cli.jobs if job["id"] != "customer")
    assert next(job for job in cli.jobs if job["id"] == "customer") == unknown
    assert not any(
        call[:2] == ["cron", "edit"] and call[2] == "customer"
        for call in cli.calls
    )
    receipt = item.store.read()
    assert receipt["activationFailSafeStarted"] is True
    assert receipt["activationFailSafeComplete"] is False
    assert set(receipt["activationFailSafeDisabledCronIds"]) == {
        "new-incremental", "new-snapshot",
    }


@pytest.mark.parametrize("enabled_count", [1, 2])
def test_activation_failure_after_first_or_second_enable_disables_every_new_job_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled_count: int,
) -> None:
    class FailNthEnableCli(StatefulCronCli):
        enable_count = 0

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            result = super().run(args, timeout=timeout, check=check)
            if args[:2] == ["cron", "edit"] and "--enable" in args:
                self.enable_count += 1
                if self.enable_count == enabled_count:
                    raise RuntimeError(f"activation edit {enabled_count} failed")
            return result

    cli = FailNthEnableCli()
    item = manager(tmp_path / str(enabled_count), cli)
    arm_activation_fail_safe_transaction(
        item, cli, enabled_keys=set(),
    )
    with pytest.raises(RuntimeError, match=f"activation edit {enabled_count} failed"):
        item._enable_recurring_jobs(["new-incremental", "new-snapshot"])
    monkeypatch.setattr(
        item,
        "_snapshot_root_from_transaction",
        lambda _transaction: (_ for _ in ()).throw(
            RuntimeError("strict rollback continued after fail-safe")
        ),
    )

    with pytest.raises(RuntimeError, match="strict rollback continued"):
        item._rollback_locked(require_exact_post_config=False)

    managed = [job for job in cli.jobs if job["id"] != "customer"]
    assert len(managed) == 2
    assert all(job["enabled"] is False for job in managed)
    receipt = item.store.read()
    assert receipt["activationFailSafeComplete"] is True
    assert set(receipt["activationFailSafeDisabledCronIds"]) == {
        "new-incremental", "new-snapshot",
    }


@pytest.mark.parametrize("failure_point", ["health", "activation-verify"])
def test_post_activation_failure_disables_all_managed_despite_unknown_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / failure_point, cli)
    _, original_unknown = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys=set(),
        include_initial=True,
    )
    pending = item.store.read()
    pending["phase"] = "activation_pending"
    pending.pop("failurePhase", None)
    item.store.write(pending)
    item._enable_recurring_jobs(["new-incremental", "new-snapshot"])
    item._enable_initial_job("new-initial")
    assert all(job["enabled"] is True for job in cli.jobs if job["id"] != "customer")
    unknown = next(job for job in cli.jobs if job["id"] == "customer")
    unknown["description"] = "Unrelated concurrent customer edit."
    unknown_after_drift = json.loads(json.dumps(unknown))
    if failure_point == "health":
        monkeypatch.setattr(
            item,
            "_write_health_receipt",
            lambda **_: (_ for _ in ()).throw(RuntimeError("health write failed")),
        )
        with pytest.raises(RuntimeError, match="health write failed"):
            item._write_health_receipt(event="initial", status="pending")
    else:
        monkeypatch.setattr(
            item,
            "_verify_activation_pending",
            lambda _transaction: (_ for _ in ()).throw(
                RuntimeError("activation verification failed")
            ),
        )
        with pytest.raises(RuntimeError, match="activation verification failed"):
            item._verify_activation_pending(item.store.read())
    failed = item.store.read()
    failed["failurePhase"] = failed["phase"]
    failed["phase"] = "failed"
    item.store.write(failed)
    monkeypatch.setattr(
        item, "_snapshot_root_from_transaction", lambda _transaction: item.snapshot_root,
    )
    monkeypatch.setattr(
        item, "_project_root_from_transaction", lambda _transaction: item.paths.project_root,
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_preflight_rollback_assets", lambda *_, **__: {})
    monkeypatch.setattr(item, "_preflight_created_snapshot_artifacts", lambda *_: None)

    with pytest.raises(RuntimeError, match="unknown cron receipt drifted"):
        item._rollback_locked(require_exact_post_config=False)

    managed = [job for job in cli.jobs if job["id"] != "customer"]
    assert len(managed) == 3
    assert all(job["enabled"] is False for job in managed)
    assert next(job for job in cli.jobs if job["id"] == "customer") == unknown_after_drift
    assert original_unknown != unknown_after_drift
    assert not any(
        call[:2] in (["cron", "edit"], ["cron", "rm"])
        and len(call) > 2 and call[2] == "customer"
        for call in cli.calls
    )
    assert item.store.read()["activationFailSafeComplete"] is True


def test_activation_fail_safe_resumes_after_interrupted_disable_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailOnceDuringCompensationCli(StatefulCronCli):
        disable_count = 0
        failed = False

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            result = super().run(args, timeout=timeout, check=check)
            if args[:2] == ["cron", "edit"] and "--disable" in args:
                self.disable_count += 1
                if self.disable_count == 2 and not self.failed:
                    self.failed = True
                    raise RuntimeError("interrupted activation compensation")
            return result

    cli = FailOnceDuringCompensationCli()
    item = manager(tmp_path, cli)
    arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={
            core.CRON_DECLARATION_KEY,
            core.SNAPSHOT_CRON_DECLARATION_KEY,
            core.INITIAL_CRON_DECLARATION_KEY,
        },
        include_initial=True,
    )
    monkeypatch.setattr(
        item,
        "_snapshot_root_from_transaction",
        lambda _transaction: (_ for _ in ()).throw(RuntimeError("strict rollback resumed")),
    )

    with pytest.raises(RuntimeError, match="compensation was incomplete"):
        item._rollback_locked(require_exact_post_config=False)
    partial = item.store.read()
    assert partial["activationFailSafeComplete"] is False
    assert set(partial["activationFailSafeDisabledCronIds"]) == {
        "new-incremental", "new-snapshot", "new-initial",
    }

    with pytest.raises(RuntimeError, match="strict rollback resumed"):
        item._rollback_locked(require_exact_post_config=False)

    assert all(job["enabled"] is False for job in cli.jobs if job["id"] != "customer")
    completed = item.store.read()
    assert completed["activationFailSafeComplete"] is True
    assert set(completed["activationFailSafeDisabledCronIds"]) == {
        "new-incremental", "new-snapshot", "new-initial",
    }


@pytest.mark.parametrize("fault", ["missing-first", "drift-first"])
def test_activation_fail_safe_continues_after_one_target_is_unrecoverable(
    tmp_path: Path, fault: str,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / fault, cli)
    _, unknown = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={core.CRON_DECLARATION_KEY, core.SNAPSHOT_CRON_DECLARATION_KEY},
    )
    if fault == "missing-first":
        cli.jobs = [job for job in cli.jobs if job["id"] != "new-incremental"]
    else:
        first = next(job for job in cli.jobs if job["id"] == "new-incremental")
        first["description"] = "Concurrent lifecycle drift."

    with pytest.raises(RuntimeError, match="compensation was incomplete"):
        item._disable_uncommitted_managed_jobs_for_activation_failure(
            item.store.read()
        )

    assert next(
        job for job in cli.jobs if job["id"] == "new-snapshot"
    )["enabled"] is False
    assert next(job for job in cli.jobs if job["id"] == "customer") == unknown
    assert not any(
        call[:2] in (["cron", "edit"], ["cron", "rm"])
        and len(call) > 2 and call[2] == "customer"
        for call in cli.calls
    )
    receipt = item.store.read()
    assert receipt["activationFailSafeComplete"] is False
    assert "new-snapshot" in receipt["activationFailSafeDisabledCronIds"]


def test_activation_fail_safe_continues_after_one_exact_edit_fails(
    tmp_path: Path,
) -> None:
    class FailFirstDisableCli(StatefulCronCli):
        failed = False

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            if args == ["cron", "edit", "new-incremental", "--disable"] \
                    and not self.failed:
                self.failed = True
                raise RuntimeError("first exact disable failed")
            return super().run(args, timeout=timeout, check=check)

    cli = FailFirstDisableCli()
    item = manager(tmp_path, cli)
    _, unknown = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={core.CRON_DECLARATION_KEY, core.SNAPSHOT_CRON_DECLARATION_KEY},
    )

    with pytest.raises(RuntimeError, match="compensation was incomplete"):
        item._disable_uncommitted_managed_jobs_for_activation_failure(
            item.store.read()
        )

    assert next(
        job for job in cli.jobs if job["id"] == "new-incremental"
    )["enabled"] is True
    assert next(
        job for job in cli.jobs if job["id"] == "new-snapshot"
    )["enabled"] is False
    assert next(job for job in cli.jobs if job["id"] == "customer") == unknown
    assert item.store.read()["activationFailSafeComplete"] is False


def test_committed_armed_final_verify_failure_disables_jobs_before_unknown_drift_blocks_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    transaction, _ = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={core.CRON_DECLARATION_KEY, core.SNAPSHOT_CRON_DECLARATION_KEY},
    )
    transaction["phase"] = "committed"
    transaction.pop("failurePhase", None)
    item.store.write(transaction)
    unknown_after_drift: dict[str, Any] = {}

    def fail_final_verify() -> dict[str, Any]:
        unknown = next(job for job in cli.jobs if job["id"] == "customer")
        unknown["description"] = "Unknown drift during final verify."
        unknown_after_drift.update(json.loads(json.dumps(unknown)))
        raise RuntimeError("final committed verify failed")

    monkeypatch.setattr(item, "verify", fail_final_verify)
    monkeypatch.setattr(
        item, "_snapshot_root_from_transaction", lambda _transaction: item.snapshot_root,
    )
    monkeypatch.setattr(
        item, "_project_root_from_transaction", lambda _transaction: item.paths.project_root,
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_preflight_rollback_assets", lambda *_, **__: {})
    monkeypatch.setattr(item, "_preflight_created_snapshot_artifacts", lambda *_: None)

    with pytest.raises(core.IntegrationRollbackIncomplete) as caught:
        item._resume_armed_committed_transaction(transaction)

    assert "final committed verify failed" in str(caught.value.original_error)
    assert "unknown cron receipt drifted" in str(caught.value.rollback_error)
    assert all(job["enabled"] is False for job in cli.jobs if job["id"] != "customer")
    assert next(job for job in cli.jobs if job["id"] == "customer") == unknown_after_drift
    assert not any(call[:3] == ["cron", "rm", "customer"] for call in cli.calls)


def test_committed_armed_disarm_crash_is_resumed_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    transaction, _ = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={core.CRON_DECLARATION_KEY, core.SNAPSHOT_CRON_DECLARATION_KEY},
    )
    transaction["phase"] = "committed"
    transaction.pop("failurePhase", None)
    item.store.write(transaction)
    monkeypatch.setattr(item, "verify", lambda: {"ok": True, "phase": "committed"})
    real_write = item.store.write
    crashed = False

    def crash_before_first_disarm(payload: dict[str, Any]) -> Path:
        nonlocal crashed
        if payload.get("phase") == "committed" \
                and payload.get("activationFailSafeRequired") is False \
                and not crashed:
            crashed = True
            raise SimulatedCrash("process stopped before durable disarm")
        return real_write(payload)

    monkeypatch.setattr(item.store, "write", crash_before_first_disarm)
    with pytest.raises(SimulatedCrash):
        item._resume_armed_committed_transaction(transaction)

    assert item.store.read()["phase"] == "committed"
    assert item.store.read()["activationFailSafeRequired"] is True
    monkeypatch.setattr(item.store, "write", real_write)

    result = item._integrate_locked({"runtimePort": 18888})

    assert result["transaction"] == "already_current"
    assert item.store.read()["activationFailSafeRequired"] is False
    assert all(job["enabled"] is True for job in cli.jobs if job["id"] != "customer")


def test_applied_then_raised_disarm_rearms_before_failed_second_verify_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    transaction, _ = arm_activation_fail_safe_transaction(
        item,
        cli,
        enabled_keys={core.CRON_DECLARATION_KEY, core.SNAPSHOT_CRON_DECLARATION_KEY},
    )
    transaction["phase"] = "committed"
    transaction.pop("failurePhase", None)
    item.store.write(transaction)
    real_write = item.store.write
    disarm_raised = False
    verify_calls = 0
    unknown_after_drift: dict[str, Any] = {}

    def applied_then_raised(payload: dict[str, Any]) -> Path:
        nonlocal disarm_raised
        if payload.get("phase") == "committed" \
                and payload.get("activationFailSafeRequired") is False \
                and not disarm_raised:
            disarm_raised = True
            real_write(payload)
            raise OSError("simulated post-replace disarm write error")
        return real_write(payload)

    def verify_twice() -> dict[str, Any]:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            return {"ok": True, "phase": "committed"}
        unknown = next(job for job in cli.jobs if job["id"] == "customer")
        unknown["description"] = "Unknown drift before ambiguous disarm recovery."
        unknown_after_drift.update(json.loads(json.dumps(unknown)))
        raise RuntimeError("second committed verification failed")

    monkeypatch.setattr(item.store, "write", applied_then_raised)
    monkeypatch.setattr(item, "verify", verify_twice)
    monkeypatch.setattr(
        item, "_snapshot_root_from_transaction", lambda _transaction: item.snapshot_root,
    )
    monkeypatch.setattr(
        item, "_project_root_from_transaction", lambda _transaction: item.paths.project_root,
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_preflight_rollback_assets", lambda *_, **__: {})
    monkeypatch.setattr(item, "_preflight_created_snapshot_artifacts", lambda *_: None)

    with pytest.raises(core.IntegrationRollbackIncomplete) as caught:
        item._resume_armed_committed_transaction(transaction)

    assert disarm_raised is True
    assert verify_calls == 2
    assert "second committed verification failed" in str(caught.value.original_error)
    assert "unknown cron receipt drifted" in str(caught.value.rollback_error)
    assert all(job["enabled"] is False for job in cli.jobs if job["id"] != "customer")
    assert next(job for job in cli.jobs if job["id"] == "customer") == unknown_after_drift
    assert not any(
        call[:2] == ["cron", "edit"] and call[2] == "customer"
        for call in cli.calls
    )
    receipt = item.store.read()
    assert receipt["phase"] == "rollback_failed"
    assert receipt["activationFailSafeRequired"] is True
    assert receipt["activationFailSafeComplete"] is True


def test_rollback_restores_owned_and_gemini_definitions_and_preserves_unknown_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    prior_incremental = job_for_spec(item._incremental_spec(), job_id="incremental", enabled=True)
    prior_gemini = gemini_job(item, job_id="gemini", enabled=True)
    unknown = customer_job()
    cli.jobs = [
        job_for_spec(item._incremental_spec(), job_id="incremental", enabled=False),
        gemini_job(item, job_id="gemini", enabled=False),
        unknown,
    ]
    prior = [core._job_definition(prior_incremental), core._job_definition(prior_gemini)]
    write_rollback_transaction(
        item,
        prior_definitions=prior,
        unknown=unknown,
        target_ids=["incremental", "gemini"],
        managed_after=[],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    result = item._rollback_locked(require_exact_post_config=False)

    assert result == {
        "ok": True,
        "status": "ROLLED_BACK",
        "outcome": "restored_exactly",
    }
    assert item.store.read()["phase"] == "rolled_back"
    after_unknown = next(job for job in cli.jobs if job["id"] == "customer")
    assert core._job_contract_hash(after_unknown, include_id=True) == core._job_contract_hash(
        unknown, include_id=True,
    )
    restored = [job for job in cli.jobs if job["id"] != "customer"]
    assert sorted(core._job_contract_hash(job) for job in restored) == sorted(
        core._job_contract_hash(definition) for definition in prior
    )


def test_rollback_rejects_command_tools_receipt_before_cron_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    prior = job_for_spec(item._incremental_spec(), job_id="incremental", enabled=True)
    prior["payload"]["toolsAllow"] = ["exec"]
    unknown = customer_job()
    cli.jobs = [
        job_for_spec(item._incremental_spec(), job_id="incremental", enabled=False),
        unknown,
    ]
    jobs_before = json.loads(json.dumps(cli.jobs))
    write_rollback_transaction(
        item,
        prior_definitions=[prior],
        unknown=unknown,
        target_ids=["incremental"],
        managed_after=[],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)

    with pytest.raises(RuntimeError, match="tools"):
        item._rollback_locked(require_exact_post_config=False)

    assert cli.calls == []
    assert cli.jobs == jobs_before


@pytest.mark.parametrize("fault", ["rm", "enable"])
def test_rollback_cron_mutation_failure_never_marks_transaction_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    class FaultCronCli(StatefulCronCli):
        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            if fault == "rm" and args[:2] == ["cron", "rm"]:
                raise subprocess.CalledProcessError(1, args, stderr="redacted fixture failure")
            if fault == "enable" and args[:2] == ["cron", "edit"] and "--enable" in args:
                raise subprocess.CalledProcessError(1, args, stderr="redacted fixture failure")
            return super().run(args, timeout=timeout, check=check)

    cli = FaultCronCli()
    item = manager(tmp_path, cli)
    prior_incremental = job_for_spec(item._incremental_spec(), job_id="incremental", enabled=True)
    unknown = customer_job()
    cli.jobs = [
        job_for_spec(item._incremental_spec(), job_id="incremental", enabled=False),
        unknown,
    ]
    write_rollback_transaction(
        item,
        prior_definitions=[core._job_definition(prior_incremental)],
        unknown=unknown,
        target_ids=["incremental"],
        managed_after=[],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    with pytest.raises(subprocess.CalledProcessError):
        item._rollback_locked(require_exact_post_config=False)

    assert item.store.read()["phase"] == "failed"


def test_failed_fresh_install_removes_only_recorded_empty_snapshot_root_and_lock(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    index_lock = data / "index.lock"
    index_lock.mkdir(mode=0o700)
    index_metadata = index_lock.stat()
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    lock = item.snapshot_root / ".snapshot-run.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    metadata = lock.stat()
    root_metadata = item.snapshot_root.stat()

    item._remove_created_snapshot_artifacts({
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": True,
        "indexLockDev": index_metadata.st_dev,
        "indexLockIno": index_metadata.st_ino,
        "snapshotLockCreated": True,
        "snapshotLockDev": metadata.st_dev,
        "snapshotLockIno": metadata.st_ino,
        "snapshotRootCreated": True,
        "snapshotRootDev": root_metadata.st_dev,
        "snapshotRootIno": root_metadata.st_ino,
    })

    assert not index_lock.exists()
    assert not item.snapshot_root.exists()


def test_unreceipted_planned_snapshot_root_is_preserved(tmp_path: Path) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)

    item._remove_created_snapshot_artifacts({
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreatePlanned": True,
        "snapshotRootCreated": False,
    })

    assert item.snapshot_root.is_dir()


def test_created_snapshot_root_replacement_is_refused_and_preserved(tmp_path: Path) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    original = item.snapshot_root.stat()
    replacement_path = item.snapshot_root.with_name("snapshot-root-replacement")
    replacement_path.mkdir(mode=0o700)
    replacement = replacement_path.stat()
    item.snapshot_root.rmdir()
    replacement_path.rename(item.snapshot_root)
    assert (replacement.st_dev, replacement.st_ino) != (original.st_dev, original.st_ino)

    with pytest.raises(RuntimeError, match="Created snapshot root is unsafe"):
        item._remove_created_snapshot_artifacts({
            "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
            "ownership": item._ownership_payload(),
            "indexLockCreated": False,
            "snapshotLockCreated": False,
            "snapshotRootCreated": True,
            "snapshotRootDev": original.st_dev,
            "snapshotRootIno": original.st_ino,
        })

    assert item.snapshot_root.is_dir()


def test_legacy_v2_created_snapshot_root_without_identity_is_preserved_and_receipted(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    transaction = {
        "schemaVersion": core.SCHEMA_VERSION,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": True,
    }
    item.store.write(transaction)

    item._remove_created_snapshot_artifacts(transaction)

    assert item.snapshot_root.is_dir()
    assert item.store.read()["legacyUnverifiedSnapshotRootPreserved"] is True


def test_current_created_snapshot_root_missing_identity_fails_closed(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreatePlanned": True,
        "snapshotRootCreated": True,
    }
    item.store.write(transaction)

    with pytest.raises(RuntimeError, match="identity is missing"):
        item._remove_created_snapshot_artifacts(transaction)

    assert item.snapshot_root.is_dir()


def test_created_lock_cleanup_is_idempotent_when_locks_are_already_absent(tmp_path: Path) -> None:
    item = manager(tmp_path)
    (item.paths.project_root / "data").mkdir(mode=0o700)
    item.snapshot_root.mkdir(parents=True, mode=0o700)

    item._remove_created_snapshot_artifacts({
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": True,
        "indexLockDev": 1,
        "indexLockIno": 2,
        "snapshotLockCreated": True,
        "snapshotLockDev": 3,
        "snapshotLockIno": 4,
        "snapshotRootCreated": False,
    })

    assert item.snapshot_root.is_dir()


def test_created_snapshot_cleanup_is_bound_to_transaction_ownership(tmp_path: Path) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    unrelated = item.paths.home / "unrelated-private-snapshots"
    unrelated.mkdir(mode=0o700)
    mismatched = manager(tmp_path, snapshot_root=unrelated)
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": True,
    }

    with pytest.raises(RuntimeError, match="does not match transaction ownership"):
        mismatched._remove_created_snapshot_artifacts(transaction)

    assert item.snapshot_root.is_dir()
    assert unrelated.is_dir()


def test_project_recovery_is_bound_to_transaction_ownership(tmp_path: Path) -> None:
    item = manager(tmp_path)
    ownership = item._ownership_payload()
    ownership["projectRoot"] = str(
        item.paths.workspace / "different" / "knowledge-lancedb-qwen-local"
    )

    with pytest.raises(RuntimeError, match="does not match transaction ownership"):
        item._project_root_from_transaction({
            "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
            "ownership": ownership,
        })


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash semantics")
def test_quiescence_lock_identities_survive_sigkill_before_guard_yields(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "phase": "quiescing",
        "ownership": item._ownership_payload(),
    })
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        transaction = item.store.read()

        def checkpoint(receipt: dict[str, Any]) -> None:
            transaction.update({
                key: value for key, value in receipt.items()
                if key not in {
                    "persisted", "indexLockPersisted", "snapshotLockPersisted",
                }
            })
            item.store.write(transaction)
            if "snapshotLockDev" in receipt:
                os.write(write_fd, b"ready")
                signal.pause()

        try:
            with item._runtime_quiescence_guard(checkpoint=checkpoint):
                os._exit(2)
        finally:
            os._exit(3)

    os.close(write_fd)
    try:
        assert read_pipe_with_timeout(read_fd) == b"ready"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status)
        transaction = item.store.read()
        assert transaction["indexLockCreated"] is True
        assert transaction["snapshotLockCreated"] is True
        assert isinstance(transaction["indexLockDev"], int)
        assert isinstance(transaction["indexLockIno"], int)
        assert isinstance(transaction["snapshotLockDev"], int)
        assert isinstance(transaction["snapshotLockIno"], int)
        assert (data / "index.lock").is_dir()
        assert (item.snapshot_root / ".snapshot-run.lock").is_file()

        item._remove_created_snapshot_artifacts(transaction)

        assert not (data / "index.lock").exists()
        assert not (item.snapshot_root / ".snapshot-run.lock").exists()
        assert item.snapshot_root.is_dir()
    finally:
        os.close(read_fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash semantics")
def test_index_lock_receipt_survives_sigkill_during_parent_fsync(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "phase": "quiescing",
        "ownership": item._ownership_payload(),
    })
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        transaction = item.store.read()

        def checkpoint(receipt: dict[str, Any]) -> None:
            transaction.update(receipt)
            item.store.write(transaction)
            if "indexLockDev" in receipt:
                def block_fsync(_descriptor: int) -> None:
                    os.write(write_fd, b"ready")
                    signal.pause()

                core.os.fsync = block_fsync

        try:
            with item._runtime_quiescence_guard(checkpoint=checkpoint):
                os._exit(2)
        finally:
            os._exit(3)

    os.close(write_fd)
    try:
        assert read_pipe_with_timeout(read_fd) == b"ready"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status)
        transaction = item.store.read()
        assert transaction["indexLockCreated"] is True
        assert isinstance(transaction["indexLockDev"], int)
        assert isinstance(transaction["indexLockIno"], int)
        assert (data / "index.lock").is_dir()

        item._remove_created_snapshot_artifacts(transaction)

        assert not (data / "index.lock").exists()
    finally:
        os.close(read_fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash semantics")
def test_staged_index_lock_recovers_sigkill_after_publish_before_final_checkpoint(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "phase": "quiescing",
        "ownership": item._ownership_payload(),
    })
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        transaction = item.store.read()
        real_publish = core._rename_noreplace_at

        def checkpoint(receipt: dict[str, Any]) -> None:
            transaction.update(receipt)
            item.store.write(transaction)

        def publish_then_block(
            source_fd: int, source: str, target_fd: int, target: str,
        ) -> None:
            real_publish(source_fd, source, target_fd, target)
            if target == "index.lock":
                os.write(write_fd, b"ready")
                signal.pause()

        core._rename_noreplace_at = publish_then_block
        try:
            with item._runtime_quiescence_guard(checkpoint=checkpoint):
                os._exit(2)
        finally:
            os._exit(3)

    os.close(write_fd)
    try:
        assert read_pipe_with_timeout(read_fd) == b"ready"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status)
        transaction = item.store.read()
        assert transaction["indexLockPublished"] is False
        assert transaction.get("indexLockCreated") is not True
        assert isinstance(transaction["indexLockStageDev"], int)
        assert isinstance(transaction["indexLockStageIno"], int)
        assert (data / "index.lock").is_dir()
        assert not (data / transaction["indexLockStageName"]).exists()

        item._remove_created_snapshot_artifacts(transaction)

        assert not (data / "index.lock").exists()
    finally:
        os.close(read_fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash semantics")
def test_staged_snapshot_lock_recovers_sigkill_after_publish_before_final_checkpoint(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "phase": "quiescing",
        "ownership": item._ownership_payload(),
    })
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        transaction = item.store.read()
        real_publish = core._rename_noreplace_at

        def checkpoint(receipt: dict[str, Any]) -> None:
            transaction.update(receipt)
            item.store.write(transaction)

        def publish_then_block(
            source_fd: int, source: str, target_fd: int, target: str,
        ) -> None:
            real_publish(source_fd, source, target_fd, target)
            if target == ".snapshot-run.lock":
                os.write(write_fd, b"ready")
                signal.pause()

        core._rename_noreplace_at = publish_then_block
        try:
            with item._runtime_quiescence_guard(checkpoint=checkpoint):
                os._exit(2)
        finally:
            os._exit(3)

    os.close(write_fd)
    try:
        assert read_pipe_with_timeout(read_fd) == b"ready"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status)
        transaction = item.store.read()
        assert transaction["indexLockPublished"] is True
        assert transaction["snapshotLockPublished"] is False
        assert transaction.get("snapshotLockCreated") is not True
        assert (data / "index.lock").is_dir()
        assert (item.snapshot_root / ".snapshot-run.lock").is_file()

        item._remove_created_snapshot_artifacts(transaction)

        assert not (data / "index.lock").exists()
        assert not (item.snapshot_root / ".snapshot-run.lock").exists()
    finally:
        os.close(read_fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash semantics")
def test_sigkill_while_waiting_preserves_preexisting_snapshot_lock(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    snapshot_lock = item.snapshot_root / ".snapshot-run.lock"
    snapshot_fd = os.open(
        snapshot_lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
    )
    core.fcntl.flock(snapshot_fd, core.fcntl.LOCK_EX | core.fcntl.LOCK_NB)
    snapshot_identity = snapshot_lock.stat()
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "phase": "quiescing",
        "ownership": item._ownership_payload(),
    })
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        transaction = item.store.read()

        def checkpoint(receipt: dict[str, Any]) -> None:
            transaction.update(receipt)
            item.store.write(transaction)
            if "snapshotLockDev" in receipt:
                os.write(write_fd, b"ready")

        try:
            with item._runtime_quiescence_guard(
                checkpoint=checkpoint, timeout_seconds=60, poll_seconds=0.01,
            ):
                os._exit(2)
        finally:
            os._exit(3)

    os.close(write_fd)
    try:
        assert read_pipe_with_timeout(read_fd) == b"ready"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status)
        transaction = item.store.read()
        assert transaction["indexLockCreated"] is True
        assert transaction["snapshotLockCreated"] is False
        assert transaction["snapshotLockDev"] == snapshot_identity.st_dev
        assert transaction["snapshotLockIno"] == snapshot_identity.st_ino

        item._remove_created_snapshot_artifacts(transaction)

        assert not (data / "index.lock").exists()
        assert snapshot_lock.is_file()
        after = snapshot_lock.stat()
        assert (after.st_dev, after.st_ino) == (
            snapshot_identity.st_dev, snapshot_identity.st_ino,
        )
    finally:
        core.fcntl.flock(snapshot_fd, core.fcntl.LOCK_UN)
        os.close(snapshot_fd)
        os.close(read_fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def test_recovery_rejects_replaced_index_lock_without_deleting_it(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    index_lock = data / "index.lock"
    index_lock.mkdir(mode=0o700)
    original = index_lock.stat()
    replacement_path = data / "index.lock.replacement"
    replacement_path.mkdir(mode=0o700)
    replacement = replacement_path.stat()
    index_lock.rmdir()
    replacement_path.rename(index_lock)
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": True,
        "indexLockDev": original.st_dev,
        "indexLockIno": original.st_ino,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }

    with pytest.raises(RuntimeError, match="Created index lock changed"):
        item._remove_created_snapshot_artifacts(transaction)

    assert index_lock.is_dir()


def test_recovery_removes_index_lock_published_before_final_checkpoint(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    final = data / "index.lock"
    final.mkdir(mode=0o700)
    metadata = final.stat()
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockStageName": ".index.lock.install-11111111111111111111111111111111",
        "indexLockStageDev": metadata.st_dev,
        "indexLockStageIno": metadata.st_ino,
        "indexLockPublished": False,
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }

    item._remove_created_snapshot_artifacts(transaction)

    assert not final.exists()


def test_recovery_preserves_unrelated_final_when_unpublished_stage_is_absent(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    unrelated = data / "index.lock"
    unrelated.mkdir(mode=0o700)
    unrelated_meta = unrelated.stat()
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockStageName": ".index.lock.install-22222222222222222222222222222222",
        "indexLockStageDev": unrelated_meta.st_dev,
        "indexLockStageIno": unrelated_meta.st_ino + 1000000,
        "indexLockPublished": False,
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }

    item._remove_created_snapshot_artifacts(transaction)

    assert unrelated.is_dir()


def test_recovery_rejects_replaced_unpublished_index_stage(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    stage_name = ".index.lock.install-33333333333333333333333333333333"
    stage = data / stage_name
    stage.mkdir(mode=0o700)
    original = stage.stat()
    replacement_path = data / ".index.lock.replacement"
    replacement_path.mkdir(mode=0o700)
    replacement = replacement_path.stat()
    stage.rmdir()
    replacement_path.rename(stage)
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockStageName": stage_name,
        "indexLockStageDev": original.st_dev,
        "indexLockStageIno": original.st_ino,
        "indexLockPublished": False,
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }

    with pytest.raises(RuntimeError, match="Staged runtime lock changed"):
        item._remove_created_snapshot_artifacts(transaction)

    assert stage.is_dir()


def test_recovery_rejects_replaced_snapshot_lock_without_deleting_it(tmp_path: Path) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    snapshot_lock = item.snapshot_root / ".snapshot-run.lock"
    snapshot_lock.touch(mode=0o600)
    original = snapshot_lock.stat()
    replacement_path = item.snapshot_root / ".snapshot-run.lock.replacement"
    replacement_path.touch(mode=0o600)
    replacement_path.chmod(0o600)
    replacement = replacement_path.stat()
    snapshot_lock.unlink()
    replacement_path.rename(snapshot_lock)
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": False,
        "snapshotLockCreated": True,
        "snapshotLockDev": original.st_dev,
        "snapshotLockIno": original.st_ino,
        "snapshotRootCreated": False,
    }

    with pytest.raises(RuntimeError, match="Created snapshot lock changed"):
        item._remove_created_snapshot_artifacts(transaction)

    assert snapshot_lock.is_file()


def test_recovery_removes_snapshot_lock_published_before_final_checkpoint(tmp_path: Path) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    final = item.snapshot_root / ".snapshot-run.lock"
    final.touch(mode=0o600)
    final.chmod(0o600)
    metadata = final.stat()
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "indexLockCreated": False,
        "snapshotLockStageName": ".snapshot-run.lock.install-44444444444444444444444444444444",
        "snapshotLockStageDev": metadata.st_dev,
        "snapshotLockStageIno": metadata.st_ino,
        "snapshotLockPublished": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }

    item._remove_created_snapshot_artifacts(transaction)

    assert not final.exists()


def initial_job(item: core.IntegrationManager, *, enabled: bool = True) -> dict[str, Any]:
    return {
        "id": "initial",
        "name": "Qwen local knowledge initial full index",
        "description": core.INITIAL_CRON_DESCRIPTION,
        "enabled": enabled,
        "declarationKey": core.INITIAL_CRON_DECLARATION_KEY,
        "sessionTarget": "isolated",
        "sessionKey": None,
        "agentId": None,
        "schedule": {"kind": "at", "at": "2026-09-03T23:00:00.000Z"},
        "payload": {
            "kind": "command",
            "argv": [
                str(item.paths.project_root / "scripts/knowledge_index_full.sh"),
                str(item.ownership_manifest),
            ],
            "cwd": str(item.paths.project_root),
            "timeoutSeconds": 86400,
            "noOutputTimeoutSeconds": 1800,
            "outputMaxBytes": 65536,
            "env": {
                "QWEN_OWNERSHIP_MANIFEST": str(item.ownership_manifest),
                "QWEN_PYTHON": str(item.python_path),
                "OPENCLAW_LANCEDB_ROOT": str(item.paths.project_root),
            },
        },
        "delivery": {"mode": "none"},
        "deleteAfterRun": True,
        "failureAlert": {
            "after": 1,
            "cooldownMs": 3600000,
            "includeSkipped": False,
            "mode": "announce",
            "channel": item.report_channel,
            "to": item.report_to,
            "accountId": item.report_account_id,
        },
    }


def test_initial_job_and_health_receipt_are_verified_as_exact_contracts(tmp_path: Path) -> None:
    item = manager(tmp_path)
    expected = initial_job(item)
    assert item._initial_job_matches(expected, enabled=True)

    cli_normalized = json.loads(json.dumps(expected))
    cli_normalized["delivery"] = {"mode": "none", "channel": "last"}
    assert item._initial_job_matches(cli_normalized, enabled=True)

    drifted = json.loads(json.dumps(expected))
    drifted["payload"]["env"]["EXTRA"] = "drift"
    assert not item._initial_job_matches(drifted, enabled=True)

    receipt = {
        "schema": core.HEALTH_RECEIPT_SCHEMA,
        "component": "qwen-local",
        "producer": "qwen-local",
        "declarationKey": core.SNAPSHOT_CRON_DECLARATION_KEY,
        "status": "ok",
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "freshness": {
            "status": "current",
            "maxAgeSeconds": core.HEALTH_RECEIPT_MAX_AGE_SECONDS,
        },
        "summary": "Qwen 本機索引與快照健康",
        "checks": [{"key": "snapshot", "status": "ok", "summary": "驗證完成"}],
        "metrics": {"rows": 42},
        "anomalies": [],
        "pending": [],
    }
    item.health_receipt_path.parent.mkdir(parents=True)
    item.health_receipt_path.parent.chmod(0o700)
    item.health_receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    item.health_receipt_path.chmod(0o600)
    assert item._health_receipt_status() == "ok"

    receipt["freshness"]["maxAgeSeconds"] += 1
    item.health_receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert item._health_receipt_status() == "warning"
    receipt["freshness"]["maxAgeSeconds"] -= 1
    receipt["anomalies"] = [{
        "code": "TEST", "summary": "test", "impact": "none",
        "dataLoss": False, "repairStatus": "done",
    }]
    item.health_receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert item._health_receipt_status() == "warning"

    receipt["anomalies"] = []
    receipt["unexpected"] = "not part of the consumer contract"
    item.health_receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert item._health_receipt_status() == "warning"

    receipt.pop("unexpected")
    receipt["checkedAt"] = datetime.now().isoformat()
    item.health_receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert item._health_receipt_status() == "warning"

    receipt["checkedAt"] = datetime.now(timezone.utc).isoformat()
    item.health_receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    linked = item.health_receipt_path.with_name("receipt-hardlink.json")
    os.link(item.health_receipt_path, linked)
    assert item._health_receipt_status() == "warning"


def test_local_source_map_rejects_loopback_userinfo_confusion(tmp_path: Path) -> None:
    item = manager(tmp_path)
    source_map = item.paths.project_root / "config/source-map.json"
    source_map.parent.mkdir(parents=True, exist_ok=True)
    source_map.write_text(json.dumps({
        "embedding": {
            "provider": "qwen-local",
            "endpoint": "http://127.0.0.1:18888@external.invalid",
        }
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="loopback-only"):
        item._verify_local_source_map()

    source_map.write_text(json.dumps({
        "embedding": {"provider": "qwen-local", "endpoint": "http://127.0.0.1:18888"}
    }), encoding="utf-8")
    item._verify_local_source_map()


class RestoreRecordingCli:
    def __init__(self) -> None:
        self.json_calls: list[list[str]] = []
        self.run_calls: list[list[str]] = []

    def json(self, args: list[str], *, timeout: int = 120) -> Any:
        self.json_calls.append(list(args))
        return {"id": "restored"}

    def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
        self.run_calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")


def test_cron_rollback_receipt_preserves_one_shot_and_alert_definition(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    definition = initial_job(item, enabled=True)
    receipt = core._job_definition(definition)
    transaction = write_staging_transaction(item, [], set())

    assert receipt["deleteAfterRun"] is True
    restored_id = item._restore_cron_definition(receipt, transaction)
    add = next(call for call in cli.calls if call[:2] == ["cron", "add"])
    assert add[add.index("--at") + 1] == definition["schedule"]["at"]
    assert "--delete-after-run" in add and "--disabled" in add and "--no-deliver" in add
    alert = next(call for call in cli.calls if "--failure-alert" in call)
    assert alert[alert.index("--failure-alert-mode") + 1] == "announce"
    assert "--failure-alert-exclude-skipped" in alert
    assert not any(call[:3] == ["cron", "edit", restored_id] and "--enable" in call
                   for call in cli.calls)
    assert next(job for job in cli.jobs if job["id"] == restored_id)["enabled"] is False


@pytest.mark.parametrize("enabled", [True, False])
def test_cron_rollback_round_trips_legacy_definition_exactly(
    tmp_path: Path, enabled: bool,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    original = legacy_snapshot_job(item, enabled=enabled)
    definition = core._job_definition(original)
    transaction = write_staging_transaction(item, [], set())

    restored_id = item._restore_cron_definition(definition, transaction)

    restored = next(job for job in cli.jobs if job["id"] == restored_id)
    expected = json.loads(json.dumps(definition))
    expected["enabled"] = False
    assert core._job_contract_hash(restored) == core._job_contract_hash(expected)
    add = next(call for call in cli.calls if call[:2] == ["cron", "add"])
    assert "--disabled" in add
    assert add[add.index("--declaration-key") + 1] == core.LEGACY_SNAPSHOT_DECLARATION_KEY
    assert not any(call[:3] == ["cron", "edit", restored_id] and "--enable" in call
                   for call in cli.calls)


def test_configure_openclaw_force_replaces_existing_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConfigureCli:
        def __init__(self) -> None:
            self.executable = str(Path(sys.executable).resolve())
            self.calls: list[list[str]] = []

        def config_get(self, _path: str) -> list[str]:
            return []

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            self.calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

    cli = ConfigureCli()
    item = manager(tmp_path, cli)
    archive = tmp_path / "plugin-package" / "plugin.tgz"
    archive.parent.mkdir()
    archive.write_bytes(b"fixture")
    monkeypatch.setattr(item, "package_plugin_archive", lambda: archive)

    item.configure_openclaw([])

    install = next(call for call in cli.calls if call[:2] == ["plugins", "install"])
    assert install == ["plugins", "install", "--force", str(archive)]
    assert not archive.parent.exists()


def _write_precise_runtime_transaction(
    item: core.IntegrationManager,
    *,
    cron_mutation_started: bool = True,
    **markers: Any,
) -> dict[str, Any]:
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    snapshot_dir = item.paths.state_root / "snapshots/run-00000000-0000-0000-0000-000000000099"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    config_backup = snapshot_dir / "openclaw-config.preinstall"
    config_backup.write_text("{}", encoding="utf-8")
    config_backup.chmod(0o600)
    transaction: dict[str, Any] = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "runId": "precise-runtime-fixture",
        "phase": "failed",
        "ownedAssets": [],
        "configPath": str(config),
        "configBackupPath": str(config_backup),
        "preConfigSha256": "1" * 64,
        "snapshotRunMarkerSha256": "2" * 64,
        "snapshotRunDev": 1,
        "snapshotRunIno": 2,
        "runtimeMutationStarted": True,
        "pluginMutationStarted": False,
        "configMutationStarted": False,
        "skillMutationStarted": False,
        "plistMutationStarted": False,
        "launchdMutationStarted": False,
        "projectExisted": False,
        "projectCreated": False,
        "projectBackupPath": str(snapshot_dir / "project-runtime.preinstall"),
        "healthReceiptExisted": False,
        "cronMutationStarted": cron_mutation_started,
        "cronDefinitionsBefore": [],
        "cronUnknownHashesBefore": {},
        "cronInventoryHashesBefore": {},
        "cronTargetIdsBefore": [],
        "managedCronIdsAfter": [],
        "snapshotRootCreated": False,
        "snapshotLockCreated": False,
        **markers,
    }
    item.store.write(transaction)
    return transaction


def test_contract_v1_rollback_fails_closed_before_any_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    transaction = _write_precise_runtime_transaction(item)
    transaction["contractVersion"] = 1
    transaction.pop("ownership", None)
    item.store.write(transaction)
    snapshot_checked = False

    def verify_snapshot(*_args: Any, **_kwargs: Any) -> None:
        nonlocal snapshot_checked
        snapshot_checked = True

    monkeypatch.setattr(item, "_verify_config_snapshot", verify_snapshot)

    with pytest.raises(RuntimeError, match="Legacy rollback receipt lacks exact"):
        item._rollback_locked(require_exact_post_config=False)

    assert snapshot_checked is False
    assert item.cli.calls == []
    assert item.paths.project_root.is_dir()


def test_legacy_v2_created_project_root_without_identity_is_preserved_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    _write_precise_runtime_transaction(item, projectCreated=True)
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_args, **_kwargs: None)

    result = item._rollback_locked(require_exact_post_config=False)

    assert result == {
        "ok": True,
        "status": "ROLLED_BACK",
        "outcome": "restored_with_preserved_artifacts",
    }
    assert item.paths.project_root.is_dir()
    receipt = item.store.read()
    assert receipt["legacyUnverifiedProjectRootPreserved"] is True
    assert "projectRoot" in receipt["rollbackPreservedResources"]


def test_current_created_project_root_missing_identity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    _write_precise_runtime_transaction(
        item,
        projectCreatePlanned=True,
        projectCreated=True,
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="project identity is missing"):
        item._rollback_locked(require_exact_post_config=False)

    assert item.paths.project_root.is_dir()


def test_recovery_quarantines_receipted_project_root_stage(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    item.paths.project_root.rmdir()
    stage_name = ".qwen-project-root.install-11111111111111111111111111111111"
    stage = item.paths.project_root.parent / stage_name
    stage.mkdir(mode=0o700)
    metadata = stage.stat()
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "projectRootStageName": stage_name,
        "projectRootStageDev": metadata.st_dev,
        "projectRootStageIno": metadata.st_ino,
        "projectRootPublished": False,
        "projectCreated": False,
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }
    item.store.write(transaction)

    item._remove_created_snapshot_artifacts(transaction)

    assert not stage.exists()
    quarantine = item.paths.project_root.parent / transaction["projectRootQuarantineName"]
    assert quarantine.is_dir()
    assert (quarantine.stat().st_dev, quarantine.stat().st_ino) == (
        metadata.st_dev,
        metadata.st_ino,
    )


def test_recovery_quarantines_project_root_published_before_created_checkpoint(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    item.paths.project_root.chmod(0o700)
    metadata = item.paths.project_root.stat()
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "projectRootStageName": ".qwen-project-root.install-22222222222222222222222222222222",
        "projectRootStageDev": metadata.st_dev,
        "projectRootStageIno": metadata.st_ino,
        "projectRootPublished": False,
        "projectCreated": False,
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }
    item.store.write(transaction)

    item._remove_created_snapshot_artifacts(transaction)

    assert not item.paths.project_root.exists()
    assert (
        item.paths.project_root.parent / transaction["projectRootQuarantineName"]
    ).is_dir()


def test_recovery_rejects_project_stage_and_final_collision(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    stage_name = ".qwen-project-root.install-33333333333333333333333333333333"
    stage = item.paths.project_root.parent / stage_name
    stage.mkdir(mode=0o700)
    metadata = stage.stat()
    transaction = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "projectRootStageName": stage_name,
        "projectRootStageDev": metadata.st_dev,
        "projectRootStageIno": metadata.st_ino,
        "projectRootPublished": False,
        "projectCreated": False,
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }
    item.store.write(transaction)

    with pytest.raises(RuntimeError, match="collided with the final path"):
        item._remove_created_snapshot_artifacts(transaction)

    assert stage.is_dir()
    assert item.paths.project_root.is_dir()


def test_fresh_project_root_can_be_created_after_stage_recovery(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    item.paths.project_root.rmdir()
    stage_name = ".qwen-project-root.install-44444444444444444444444444444444"
    stage = item.paths.project_root.parent / stage_name
    stage.mkdir(mode=0o700)
    metadata = stage.stat()
    interrupted = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "projectRootStageName": stage_name,
        "projectRootStageDev": metadata.st_dev,
        "projectRootStageIno": metadata.st_ino,
        "projectRootPublished": False,
        "projectCreated": False,
        "indexLockCreated": False,
        "snapshotLockCreated": False,
        "snapshotRootCreated": False,
    }
    item.store.write(interrupted)
    item._remove_created_snapshot_artifacts(interrupted)

    fresh = {
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "ownership": item._ownership_payload(),
        "projectExisted": False,
        "projectCreatePlanned": True,
        "projectCreated": False,
    }
    item.store.write(fresh)
    item._prepare_project_root(fresh)

    assert item.paths.project_root.is_dir()
    assert fresh["projectCreated"] is True
    assert type(fresh["projectRootDev"]) is int
    assert type(fresh["projectRootIno"]) is int


def test_unreceipted_planned_project_root_is_preserved_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    transaction = _write_precise_runtime_transaction(
        item,
        projectCreatePlanned=True,
        projectCreated=False,
        snapshotRootCreatePlanned=True,
        snapshotRootCreated=False,
    )
    marker = item.paths.project_root / "replacement-owned-by-someone-else"
    marker.write_text("preserve", encoding="utf-8")
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)

    result = item._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert item.snapshot_root.is_dir()
    assert transaction["projectCreated"] is False


def test_created_project_root_replacement_is_refused_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    original = item.paths.project_root.stat()
    _write_precise_runtime_transaction(
        item,
        projectCreatePlanned=True,
        projectCreated=True,
        projectRootDev=original.st_dev,
        projectRootIno=original.st_ino,
    )
    replacement_path = item.paths.project_root.with_name("project-root-replacement")
    replacement_path.mkdir(mode=0o700)
    replacement = replacement_path.stat()
    item.paths.project_root.rmdir()
    replacement_path.rename(item.paths.project_root)
    assert (replacement.st_dev, replacement.st_ino) != (original.st_dev, original.st_ino)
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)

    with pytest.raises(RuntimeError, match="identity changed"):
        item._rollback_locked(require_exact_post_config=False)

    assert item.paths.project_root.is_dir()


def test_created_project_root_missing_is_idempotent_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    original = item.paths.project_root.stat()
    _write_precise_runtime_transaction(
        item,
        projectCreatePlanned=True,
        projectCreated=True,
        projectRootDev=original.st_dev,
        projectRootIno=original.st_ino,
    )
    item.paths.project_root.rmdir()
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)

    result = item._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    assert not item.paths.project_root.exists()


def test_failure_before_plugin_install_preserves_existing_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    item.plugin_target.mkdir(parents=True)
    installed = item.plugin_target / "index.js"
    installed.write_text("existing-plugin", encoding="utf-8")
    _write_precise_runtime_transaction(item)
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    result = item._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    assert installed.read_text(encoding="utf-8") == "existing-plugin"
    assert not any(call[:2] == ["plugins", "uninstall"] for call in cli.calls)


def test_plugin_snapshot_restores_exact_tree_after_forced_upgrade(
    tmp_path: Path,
) -> None:
    class PluginCli(StatefulCronCli):
        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            if args[:2] == ["plugins", "uninstall"]:
                self.calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, "", "")
            return super().run(args, timeout=timeout, check=check)

    cli = PluginCli()
    item = manager(tmp_path, cli)
    item.plugin_target.mkdir(parents=True)
    (item.plugin_target / "index.js").write_text("old", encoding="utf-8")
    nested = item.plugin_target / "dist"
    nested.mkdir()
    (nested / "runtime.js").write_text("old-runtime", encoding="utf-8")
    openclaw_package = tmp_path / "opt/homebrew/lib/node_modules/openclaw"
    openclaw_package.mkdir(parents=True)
    (openclaw_package / "package.json").write_text('{"name":"openclaw"}', encoding="utf-8")
    node_modules = item.plugin_target / "node_modules"
    node_modules.mkdir()
    (node_modules / "openclaw").symlink_to(openclaw_package, target_is_directory=True)
    snapshot_dir = item.paths.state_root / "snapshots/run-00000000-0000-0000-0000-000000000100"
    snapshot_dir.mkdir(parents=True)
    receipt = item._snapshot_other_assets(snapshot_dir)
    backup_link = snapshot_dir / "plugin.preinstall/node_modules/openclaw"
    assert backup_link.is_symlink()
    assert os.readlink(backup_link) == str(openclaw_package)
    (item.plugin_target / "index.js").write_text("new", encoding="utf-8")
    (item.plugin_target / "added.js").write_text("new-file", encoding="utf-8")
    asset = receipt["assetReceipts"]["plugin"]
    asset["mutationStarted"] = True
    receipt["pluginMutationStarted"] = True
    post = item._safe_asset_identity(
        item.plugin_target,
        kind="directory",
        label="installed plugin",
        symlink_policy=item._asset_symlink_policy,
    )
    asset.update({
        "postParentDev": item.plugin_target.parent.stat().st_dev,
        "postParentIno": item.plugin_target.parent.stat().st_ino,
        "postKind": post["kind"],
        "postDev": post["dev"],
        "postIno": post["ino"],
        "postMode": post["mode"],
        "postSha256": post["sha256"],
    })
    item.store.write(receipt)
    prepared = item._preflight_rollback_assets(receipt, snapshot_dir)
    spec, asset = prepared["plugin"]
    item._rollback_one_asset(receipt, spec, asset, snapshot_dir)

    assert (item.plugin_target / "index.js").read_text(encoding="utf-8") == "old"
    assert (item.plugin_target / "dist/runtime.js").read_text(encoding="utf-8") == "old-runtime"
    restored_link = item.plugin_target / "node_modules/openclaw"
    assert restored_link.is_symlink()
    assert os.readlink(restored_link) == str(openclaw_package)
    assert not (item.plugin_target / "added.js").exists()
    assert item._safe_tree_sha256(
        item.plugin_target,
        label="restored plugin",
        symlink_policy=item._asset_symlink_policy,
    ) == receipt[
        "pluginBackupSha256"
    ]
    assert not any(call[:2] == ["plugins", "uninstall"] for call in cli.calls)


def test_plugin_snapshot_tamper_fails_before_uninstall(
    tmp_path: Path,
) -> None:
    class PluginCli(StatefulCronCli):
        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            if args[:2] == ["plugins", "uninstall"]:
                self.calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, "", "")
            return super().run(args, timeout=timeout, check=check)

    cli = PluginCli()
    item = manager(tmp_path, cli)
    item.plugin_target.mkdir(parents=True)
    installed = item.plugin_target / "index.js"
    installed.write_text("existing", encoding="utf-8")
    snapshot_dir = item.paths.state_root / "snapshots/run-00000000-0000-0000-0000-000000000101"
    snapshot_dir.mkdir(parents=True)
    receipt = item._snapshot_other_assets(snapshot_dir)
    asset = receipt["assetReceipts"]["plugin"]
    asset["mutationStarted"] = True
    receipt["pluginMutationStarted"] = True
    post = item._safe_asset_identity(
        item.plugin_target, kind="directory", label="installed plugin",
    )
    asset.update({
        "postParentDev": item.plugin_target.parent.stat().st_dev,
        "postParentIno": item.plugin_target.parent.stat().st_ino,
        "postKind": post["kind"],
        "postDev": post["dev"],
        "postIno": post["ino"],
        "postMode": post["mode"],
        "postSha256": post["sha256"],
    })
    (snapshot_dir / "plugin.preinstall/index.js").write_text("tampered", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed|unsafe"):
        item._preflight_rollback_assets(receipt, snapshot_dir)

    assert installed.read_text(encoding="utf-8") == "existing"
    assert not any(call[:2] == ["plugins", "uninstall"] for call in cli.calls)


def test_plugin_snapshot_rejects_traversal_symlink_without_following_it(tmp_path: Path) -> None:
    item = manager(tmp_path)
    item.plugin_target.mkdir(parents=True)
    node_modules = item.plugin_target / "node_modules"
    node_modules.mkdir()
    (node_modules / "openclaw").symlink_to("../../outside", target_is_directory=True)
    outside = item.plugin_target.parent / "outside"
    outside.mkdir()
    marker = outside / "private.txt"
    marker.write_text("must-not-copy", encoding="utf-8")
    snapshot_dir = item.paths.state_root / "snapshots/run-00000000-0000-0000-0000-000000000102"
    snapshot_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="unsafe symbolic link target"):
        item._snapshot_other_assets(snapshot_dir)

    assert marker.read_text(encoding="utf-8") == "must-not-copy"
    assert not (snapshot_dir / "plugin.preinstall").exists()


def test_launchd_activation_retries_transient_error_37_and_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    attempts = {"bootstrap": 0, "kickstart": 0, "print": 0}
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def launchctl(args: list[str], *, check: bool = True):
        calls.append(list(args))
        command = args[0]
        if command == "bootout":
            return subprocess.CompletedProcess(args, 0, "", "")
        attempts[command] += 1
        return_code = 37 if command in {"bootstrap", "print"} and attempts[command] == 1 else 0
        return subprocess.CompletedProcess(args, return_code, "", "Operation already in progress")

    monkeypatch.setattr(item, "_launchctl", launchctl)
    monkeypatch.setattr(core.time, "sleep", sleeps.append)

    item.activate_launchd()

    assert attempts == {"bootstrap": 2, "kickstart": 1, "print": 2}
    assert sleeps == [core.LAUNCHD_RETRY_DELAYS_SECONDS[0]] * 2
    assert calls[-1][0] == "print"


def test_launchd_retry_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = manager(tmp_path)
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def always_busy(args: list[str], *, check: bool = True):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 37, "", "Operation already in progress")

    monkeypatch.setattr(item, "_launchctl", always_busy)
    monkeypatch.setattr(core.time, "sleep", sleeps.append)

    with pytest.raises(subprocess.CalledProcessError) as failure:
        item._launchctl_retry(["bootstrap", "gui/1", "/tmp/fixture.plist"])

    assert failure.value.returncode == 37
    assert len(calls) == len(core.LAUNCHD_RETRY_DELAYS_SECONDS) + 1
    assert sleeps == list(core.LAUNCHD_RETRY_DELAYS_SECONDS)


def test_launchd_rollback_failure_occurs_before_any_cron_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    managed = job_for_spec(item._incremental_spec(), job_id="managed", enabled=False)
    cli.jobs = [managed]
    transaction = _write_precise_runtime_transaction(
        item,
        cron_mutation_started=True,
        plistMutationStarted=True,
        launchdMutationStarted=True,
    )
    plist_backup = Path(transaction["configBackupPath"]).parent / "launchd.preinstall.plist"
    plist_backup.write_text("old-plist", encoding="utf-8")
    item.paths.launchd_plist.write_text("new-plist", encoding="utf-8")
    transaction.update({
        "plistBackupPath": str(plist_backup),
        "plistExisted": True,
        "cronContractHashVersion": core.CRON_CONTRACT_HASH_VERSION,
        "cronDefinitionsBefore": [core._job_definition(managed)],
        "cronInventoryHashesBefore": item._inventory_hashes([managed]),
        "cronUnknownHashesBefore": {},
        "cronTargetIdsBefore": ["managed"],
        "managedCronIdsAfter": [],
        "cronStagingIntents": {},
        "cronRestoreIntents": {},
        "restoredCronIdsByDeclaration": {},
    })
    item.store.write(transaction)
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "deactivate_launchd", lambda: None)
    monkeypatch.setattr(
        item, "_bootstrap_launchd_plist",
        lambda _plist: (_ for _ in ()).throw(RuntimeError("launchd restore still busy")),
    )

    with pytest.raises(RuntimeError, match="still busy"):
        item._rollback_locked(require_exact_post_config=False)

    assert item.paths.launchd_plist.read_text(encoding="utf-8") == "old-plist"
    assert cli.jobs == [managed]
    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)


def test_successful_integration_commits_after_activation_verification_and_reinstall_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = manager(tmp_path)
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    base = {
        "schemaVersion": 1, "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "runId": "success", "phase": "prepared",
        "ownedAssets": [], "configPath": str(config), "healthReceiptExisted": False,
        "projectExisted": True,
    }
    events: list[str] = []
    real_write = item.store.write

    def record_write(payload: dict[str, Any]) -> Path:
        events.append(f"write:{payload['phase']}")
        return real_write(payload)

    monkeypatch.setattr(item.store, "write", record_write)
    monkeypatch.setattr(item, "begin", lambda **_: dict(base))
    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "_prepare_snapshot_root", lambda _transaction: create_snapshot_root(item))

    @contextmanager
    def guard(*, checkpoint=None) -> Iterator[dict[str, Any]]:
        yield {"snapshotLockCreated": False, "persisted": False}

    monkeypatch.setattr(item, "_runtime_quiescence_guard", guard)
    monkeypatch.setattr(item, "bootstrap_project", lambda _: False)
    monkeypatch.setattr(item, "synchronize_project_runtime", lambda *_: events.append("sync"))
    monkeypatch.setattr(item, "_allowed_projects", lambda: [])
    monkeypatch.setattr(item, "configure_openclaw", lambda _allowed, **_: events.append("configure"))
    monkeypatch.setattr(item, "install_launchd_plist", lambda _: events.append("plist"))
    monkeypatch.setattr(item, "activate_launchd", lambda: events.append("launchd"))
    monkeypatch.setattr(item, "_apply_managed_spec", lambda spec, *_: f"job:{spec.key}")
    monkeypatch.setattr(item, "_verify_recurring_specs", lambda **_: events.append("disabled-verified"))
    monkeypatch.setattr(item, "disable_owned_gemini_jobs", lambda: [])
    monkeypatch.setattr(item, "mark_ready_or_schedule_build", lambda *_: ("READY", None))
    monkeypatch.setattr(item, "_verify_success_cron_inventory", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(item, "_verify_commit_cron_receipt", lambda *_: None)
    monkeypatch.setattr(item, "_sha256_config", lambda _: "0" * 64)
    monkeypatch.setattr(item.cli, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(item, "_enable_recurring_jobs", lambda _: events.append("recurring-enabled"))

    def write_health(**_: Any) -> None:
        item.health_receipt_path.parent.mkdir(parents=True, exist_ok=True)
        item.health_receipt_path.write_text("{}", encoding="utf-8")
        item.health_receipt_path.chmod(0o600)
        events.append("health-written")

    monkeypatch.setattr(item, "_write_health_receipt", write_health)

    def verify_pending(transaction: dict[str, Any]) -> None:
        assert transaction["phase"] == "activation_pending"
        events.append("activation-verified")

    monkeypatch.setattr(item, "_verify_activation_pending", verify_pending)
    verify_calls: list[str] = []

    def verify_while_armed() -> dict[str, bool]:
        if not verify_calls:
            assert item.store.read()["activationFailSafeRequired"] is True
        verify_calls.append("verify")
        return {"ok": True}

    monkeypatch.setattr(item, "verify", verify_while_armed)

    result = item._integrate_locked({"runtimePort": 18888})
    assert result["transaction"] == "committed"
    assert item.store.read()["activationFailSafeRequired"] is False
    assert events.index("activation-verified") < events.index("write:committed")
    mutation_count = len(events)

    again = item._integrate_locked({"runtimePort": 18888})
    assert again["transaction"] == "already_current"
    assert len(events) == mutation_count
    assert len(verify_calls) == 2


def legacy_incremental_definition(
    item: core.IntegrationManager, *, job_id: str = "legacy-incremental", enabled: bool = True,
) -> dict[str, Any]:
    job = job_for_spec(item._incremental_spec(), job_id=job_id, enabled=enabled)
    job["description"] = None
    job["payload"] = {
        "kind": "command",
        "argv": [str(item.paths.project_root / "scripts/knowledge_index_incremental.sh")],
        "cwd": str(item.paths.project_root),
        "timeoutSeconds": 7200,
        "noOutputTimeoutSeconds": 900,
        "outputMaxBytes": 65536,
    }
    job["delivery"] = {"mode": "none"}
    job["failureAlert"] = None
    return core._job_definition(job)


class ReusedPreflightIdOnAddCli(StatefulCronCli):
    def json(self, args: list[str], *, timeout: int = 120) -> Any:
        result = super().json(args, timeout=timeout)
        if args[:2] != ["cron", "add"]:
            return result
        created_id = str(result["id"])
        staged = next(job for job in self.jobs if str(job["id"]) == created_id)
        staged = json.loads(json.dumps(staged))
        staged["id"] = "customer"
        self.jobs = [
            job for job in self.jobs if str(job["id"]) not in {created_id, "customer"}
        ]
        self.jobs.append(staged)
        return {"id": "customer"}


@pytest.mark.parametrize("role", ["managed", "initial"])
def test_forged_add_id_never_becomes_rollback_deletion_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    cli = ReusedPreflightIdOnAddCli()
    item = manager(tmp_path / role, cli)
    unknown = customer_job()
    cli.jobs = [json.loads(json.dumps(unknown))]
    write_rollback_transaction(
        item, prior_definitions=[], unknown=unknown, target_ids=[], managed_after=[],
    )
    transaction = item.store.read()
    if role == "initial":
        script = item.paths.project_root / "scripts/knowledge_index_full.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o700)
        monkeypatch.setattr(
            core.subprocess, "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", ""),
        )
        invoke = lambda: item.mark_ready_or_schedule_build(transaction)
    else:
        invoke = lambda: item._apply_managed_spec(item._incremental_spec(), transaction)

    with pytest.raises(RuntimeError, match="reuses a preflight job id"):
        invoke()
    assert item.store.read().get("managedCronIdsAfter") == []
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)
    with pytest.raises(RuntimeError, match="reuses a preflight job id"):
        item._rollback_locked(require_exact_post_config=False)
    assert any(job["id"] == "customer" for job in cli.jobs)
    assert not any(call[:3] == ["cron", "rm", "customer"] for call in cli.calls)


def test_forged_restore_add_id_is_rejected_before_checkpoint_or_edit(tmp_path: Path) -> None:
    cli = ReusedPreflightIdOnAddCli()
    item = manager(tmp_path, cli)
    unknown = customer_job()
    cli.jobs = [json.loads(json.dumps(unknown))]
    transaction = write_staging_transaction(item, [unknown], set())
    definition = core._job_definition(legacy_snapshot_job(item))

    with pytest.raises(RuntimeError, match="reuses a preflight job id"):
        item._restore_cron_definition(definition, transaction)

    intent = item.store.read()["cronRestoreIntents"][core.LEGACY_SNAPSHOT_DECLARATION_KEY]
    assert "jobId" not in intent
    assert not any(call[:2] == ["cron", "edit"] for call in cli.calls)


def test_cron_add_rejects_conflicting_outer_and_nested_ids() -> None:
    with pytest.raises(RuntimeError, match="conflicting"):
        core.IntegrationManager._job_id_from_add({
            "id": "outer-job", "job": {"id": "nested-job"},
        })


def test_unsafe_inventory_id_fails_before_any_cron_mutation(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    cli.jobs = [customer_job(job_id="--all")]

    with pytest.raises(RuntimeError, match="invalid job id"):
        item._preflight_cron_inventory()

    assert cli.calls == [["cron", "list", "--all", "--json"]]


@pytest.mark.parametrize("wrapper", ["incremental", "snapshot", "initial"])
def test_shell_wrapped_unknown_owned_script_collision_fails_closed(
    tmp_path: Path, wrapper: str,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / wrapper, cli)
    paths = {
        "incremental": item.paths.project_root / "scripts/knowledge_index_incremental.sh",
        "snapshot": item.paths.project_root / "scripts/run_verified_snapshot.py",
        "initial": item.paths.project_root / "scripts/knowledge_index_full.sh",
    }
    collision = customer_job()
    collision["payload"]["argv"] = ["sh", "-lc", f"exec '{paths[wrapper]}' --fixture"]
    cli.jobs = [collision]

    with pytest.raises(RuntimeError, match="Unknown cron job targets"):
        item._preflight_cron_inventory()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wakeMode", "next-heartbeat"),
        ("displayName", "changed display"),
        ("owner", {"agentId": "other"}),
        ("trigger", {"script": "/usr/bin/true", "once": True}),
    ],
)
def test_behavior_security_fields_are_hashed_and_rejected_for_managed_jobs(
    tmp_path: Path, field: str, value: Any,
) -> None:
    baseline = customer_job()
    changed = json.loads(json.dumps(baseline))
    changed[field] = value
    assert core._job_contract_hash(changed, include_id=True) != core._job_contract_hash(
        baseline, include_id=True,
    )

    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    managed = job_for_spec(item._incremental_spec(), job_id="managed", enabled=True)
    managed[field] = value
    cli.jobs = [managed]
    with pytest.raises(RuntimeError, match="safe upgrade allowlist"):
        item._preflight_cron_inventory()
    assert cli.calls == [["cron", "list", "--all", "--json"]]


def test_restore_rejects_unrestorable_behavior_fields_before_cli_mutation(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    definition = core._job_definition(legacy_snapshot_job(item))
    definition["trigger"] = {"script": "/usr/bin/true"}
    transaction = write_staging_transaction(item, [], set())

    with pytest.raises(RuntimeError, match="behavior fields"):
        item._restore_cron_definition(definition, transaction)

    assert cli.calls == []


@pytest.mark.parametrize("phase", ["committed", "failed"])
def test_pre_intent_v3_rollback_uses_old_hash_then_preserves_full_unknown_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / phase, cli)
    prior = legacy_incremental_definition(item, job_id="prior-incremental", enabled=True)
    for field in ("wakeMode", "displayName", "owner", "trigger"):
        prior.pop(field, None)
    unknown = customer_job()
    unknown["owner"] = {"agentId": "customer-agent"}
    unknown["wakeMode"] = "next-heartbeat"
    current_incremental = job_for_spec(
        item._incremental_spec(), job_id="new-incremental", enabled=True,
    )
    current_snapshot = job_for_spec(
        item._snapshot_spec(), job_id="new-snapshot", enabled=True,
    )
    cli.jobs = [
        json.loads(json.dumps(current_incremental)),
        json.loads(json.dumps(current_snapshot)),
        json.loads(json.dumps(unknown)),
    ]
    write_rollback_transaction(
        item,
        prior_definitions=[prior],
        unknown=unknown,
        target_ids=["prior-incremental"],
        managed_after=["new-incremental", "new-snapshot"],
    )
    transaction = item.store.read()
    transaction["phase"] = phase
    transaction.pop("cronStagingIntents", None)
    transaction.pop("cronRestoreIntents", None)
    transaction.pop("restoredCronIdsByDeclaration", None)
    transaction.pop("cronContractHashVersion", None)
    transaction["cronInventoryHashesBefore"] = {
        "prior-incremental": core._legacy_v3_job_contract_hash(prior, include_id=True),
        "customer": core._legacy_v3_job_contract_hash(unknown, include_id=True),
    }
    transaction["cronUnknownHashesBefore"] = {
        "customer": core._legacy_v3_job_contract_hash(unknown, include_id=True),
    }
    item.store.write(transaction)
    unknown_full_hash = core._job_contract_hash(unknown, include_id=True)
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    result = item._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    restored = next(job for job in cli.jobs if job["id"] != "customer")
    assert restored["description"] is None and restored["enabled"] is True
    assert core._job_contract_hash(unknown, include_id=True) == unknown_full_hash
    assert core._job_contract_hash(
        next(job for job in cli.jobs if job["id"] == "customer"), include_id=True,
    ) == unknown_full_hash


def test_pre_intent_v3_unattributed_add_window_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    prior = legacy_incremental_definition(item, job_id="prior-incremental")
    for field in ("wakeMode", "displayName", "owner", "trigger"):
        prior.pop(field, None)
    unknown = customer_job()
    orphan = job_for_spec(item._incremental_spec(), job_id="unattributed", enabled=False)
    cli.jobs = [json.loads(json.dumps(orphan)), json.loads(json.dumps(unknown))]
    write_rollback_transaction(
        item,
        prior_definitions=[prior],
        unknown=unknown,
        target_ids=["prior-incremental"],
        managed_after=[],
    )
    transaction = item.store.read()
    transaction.pop("cronStagingIntents", None)
    transaction.pop("cronRestoreIntents", None)
    transaction["cronInventoryHashesBefore"] = {
        "prior-incremental": core._legacy_v3_job_contract_hash(prior, include_id=True),
        "customer": core._legacy_v3_job_contract_hash(unknown, include_id=True),
    }
    transaction["cronUnknownHashesBefore"] = {
        "customer": core._legacy_v3_job_contract_hash(unknown, include_id=True),
    }
    item.store.write(transaction)
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    with pytest.raises(RuntimeError, match="unattributed managed cron"):
        item._rollback_locked(require_exact_post_config=False)

    assert any(job["id"] == "unattributed" for job in cli.jobs)
    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)


def test_descriptionless_legacy_restore_stays_disabled_until_global_activation(
    tmp_path: Path,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    definition = legacy_incremental_definition(item, enabled=True)
    transaction = write_staging_transaction(item, [], set())

    restored_id = item._restore_cron_definition(definition, transaction)

    add = next(call for call in cli.calls if call[:2] == ["cron", "add"])
    assert "--description" not in add
    assert next(job for job in cli.jobs if job["id"] == restored_id)["enabled"] is False
    item._activate_restored_cron_definitions(
        prior_definitions=[definition], restored_ids=[restored_id], unknown_hashes_before={},
    )
    restored = next(job for job in cli.jobs if job["id"] == restored_id)
    assert restored["enabled"] is True and restored["description"] is None


def test_second_restore_failure_never_enables_first_restored_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailSecondAddCli(StatefulCronCli):
        adds = 0

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:2] == ["cron", "add"]:
                self.adds += 1
                if self.adds == 2:
                    raise RuntimeError("second restore failed")
            return super().json(args, timeout=timeout)

    cli = FailSecondAddCli()
    item = manager(tmp_path, cli)
    unknown = customer_job()
    incremental = core._job_definition(
        job_for_spec(item._incremental_spec(), job_id="old-incremental", enabled=True)
    )
    snapshot = core._job_definition(
        job_for_spec(item._snapshot_spec(), job_id="old-snapshot", enabled=True)
    )
    cli.jobs = [json.loads(json.dumps(unknown))]
    write_rollback_transaction(
        item,
        prior_definitions=[incremental, snapshot],
        unknown=unknown,
        target_ids=["old-incremental", "old-snapshot"],
        managed_after=[],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    with pytest.raises(RuntimeError, match="second restore failed"):
        item._rollback_locked(require_exact_post_config=False)

    restored = [job for job in cli.jobs if job["id"] != "customer"]
    assert len(restored) == 1 and restored[0]["enabled"] is False
    assert not any(call[:2] == ["cron", "edit"] and "--enable" in call for call in cli.calls)


def test_global_restore_activation_failure_compensates_every_job_to_disabled(
    tmp_path: Path,
) -> None:
    class FailSecondEnableCli(StatefulCronCli):
        enables = 0

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            result = super().run(args, timeout=timeout, check=check)
            if args[:2] == ["cron", "edit"] and "--enable" in args:
                self.enables += 1
                if self.enables == 2:
                    raise RuntimeError("second enable failed")
            return result

    cli = FailSecondEnableCli()
    item = manager(tmp_path, cli)
    definitions = [
        core._job_definition(
            job_for_spec(item._incremental_spec(), job_id="old-incremental", enabled=True)
        ),
        core._job_definition(
            job_for_spec(item._snapshot_spec(), job_id="old-snapshot", enabled=True)
        ),
    ]
    transaction = write_staging_transaction(item, [], set())
    restored_ids = [item._restore_cron_definition(definition, transaction)
                    for definition in definitions]

    with pytest.raises(RuntimeError, match="second enable failed"):
        item._activate_restored_cron_definitions(
            prior_definitions=definitions,
            restored_ids=restored_ids,
            unknown_hashes_before={},
        )

    assert all(job["enabled"] is False for job in cli.jobs)


def commit_cron_receipt_fixture() -> dict[str, Any]:
    unknown = {"unknown": "a" * 64}
    gemini = {"gemini": "b" * 64}
    return {
        "cronContractHashVersion": core.CRON_CONTRACT_HASH_VERSION,
        "managedCronIdsAfter": ["managed-one", "managed-two"],
        "cronUnknownHashesBefore": dict(unknown),
        "cronCommitUnknownHashes": dict(unknown),
        "cronPreservedGeminiHashesAfterQuiesce": dict(gemini),
        "cronCommitGeminiHashes": dict(gemini),
        "cronCommitTopologyVerified": True,
        "cronCommitInventoryHashes": {
            "managed-one": "c" * 64,
            "managed-two": "d" * 64,
            **unknown,
            **gemini,
        },
    }


def test_commit_cron_receipt_rejects_nonhex_and_overlapping_authority() -> None:
    valid = commit_cron_receipt_fixture()
    assert core.IntegrationManager._validate_commit_cron_receipt_metadata(valid)

    nonhex = json.loads(json.dumps(valid))
    nonhex["cronCommitInventoryHashes"]["managed-one"] = "x" * 64
    with pytest.raises(RuntimeError, match="malformed"):
        core.IntegrationManager._validate_commit_cron_receipt_metadata(nonhex)

    subset_mismatch = json.loads(json.dumps(valid))
    subset_mismatch["cronCommitInventoryHashes"]["unknown"] = "e" * 64
    with pytest.raises(RuntimeError, match="inconsistent"):
        core.IntegrationManager._validate_commit_cron_receipt_metadata(subset_mismatch)

    overlap = json.loads(json.dumps(valid))
    overlap["cronUnknownHashesBefore"]["managed-one"] = "c" * 64
    overlap["cronCommitUnknownHashes"]["managed-one"] = "c" * 64
    with pytest.raises(RuntimeError, match="inconsistent"):
        core.IntegrationManager._validate_commit_cron_receipt_metadata(overlap)


def test_create_incremental_cron_compatibility_lookup_never_mutates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "phase": "committed",
        "cronId": "committed-incremental",
    })
    monkeypatch.setattr(item, "verify", lambda: {"ok": True})

    assert item.create_incremental_cron() == "committed-incremental"
    assert cli.calls == []

    transaction = item.store.read()
    transaction["phase"] = "failed"
    item.store.write(transaction)
    with pytest.raises(RuntimeError, match="transactional integration workflow"):
        item.create_incremental_cron()
    assert cli.calls == []


@pytest.mark.parametrize(
    "fault",
    [
        "non-string-expr", "nonzero-stagger", "invalid-timezone", "webhook-delivery",
        "main-session", "string-alert-after", "string-timeout", "future-top-level",
        "env-secret",
    ],
)
def test_malformed_gemini_contract_fails_before_transaction_or_cron_mutation(
    tmp_path: Path, fault: str,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / fault, cli)
    job = gemini_job(item)
    if fault == "non-string-expr":
        job["schedule"]["expr"] = 123
    elif fault == "nonzero-stagger":
        job["schedule"]["staggerMs"] = 5000
    elif fault == "invalid-timezone":
        job["schedule"]["tz"] = " Invalid/Timezone "
    elif fault == "webhook-delivery":
        job["delivery"] = {"mode": "webhook", "to": "https://example.invalid"}
    elif fault == "main-session":
        job["sessionTarget"] = "main"
    elif fault == "string-alert-after":
        job["failureAlert"] = {
            "after": "1", "cooldownMs": 3600000, "includeSkipped": False,
            "channel": "discord", "to": "channel:1493072746702311474",
        }
    elif fault == "string-timeout":
        job["payload"]["timeoutSeconds"] = "7200"
    elif fault == "future-top-level":
        job["futureBehavior"] = {"mode": "unreviewed"}
    elif fault == "env-secret":
        job["payload"]["env"] = {"FOO": "my-company-password"}
    cli.jobs = [job]

    with pytest.raises(RuntimeError, match="safe upgrade allowlist"):
        item._integrate_locked({"runtimePort": 18888})

    assert not item.store.manifest_path.exists()
    assert cli.calls == [["cron", "list", "--all", "--json"]]


def test_job_definition_normalizes_empty_env_and_no_delivery_semantics(tmp_path: Path) -> None:
    item = manager(tmp_path)
    job = gemini_job(item)
    job["payload"]["env"] = {}
    job["delivery"] = {"mode": "none", "channel": "last"}
    job["deleteAfterRun"] = None

    definition = core._job_definition(job)

    assert "env" not in definition["payload"]
    assert definition["delivery"] == {"mode": "none"}
    assert definition["deleteAfterRun"] is False
    without_explicit_false = json.loads(json.dumps(job))
    without_explicit_false.pop("deleteAfterRun")
    assert core._job_contract_hash(job) == core._job_contract_hash(without_explicit_false)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda job: job.update(name=" --unsafe"),
        lambda job: job["payload"]["argv"].append("bad\x00value"),
        lambda job: job["payload"].update(env={"SAFE": "bad\x00value"}),
    ],
)
def test_job_definition_rejects_cli_ambiguous_or_nul_values(
    tmp_path: Path, mutation: Any,
) -> None:
    item = manager(tmp_path)
    job = gemini_job(item)
    mutation(job)
    with pytest.raises(RuntimeError):
        core._job_definition(job)


def test_future_top_level_field_is_hashed_for_unknown_and_rejected_for_owned(
    tmp_path: Path,
) -> None:
    baseline = customer_job()
    changed = json.loads(json.dumps(baseline))
    changed["futureBehavior"] = {"mode": "new"}
    assert core._job_contract_hash(changed, include_id=True) != core._job_contract_hash(
        baseline, include_id=True,
    )

    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    managed = job_for_spec(item._incremental_spec(), job_id="managed", enabled=True)
    managed["futureBehavior"] = {"mode": "new"}
    cli.jobs = [managed]
    with pytest.raises(RuntimeError, match="safe upgrade allowlist"):
        item._preflight_cron_inventory()
    assert not any(call[:2] in (["cron", "rm"], ["cron", "edit"])
                   for call in cli.calls)


def test_env_wrapped_owned_script_collision_fails_closed(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    collision = customer_job()
    collision["payload"]["argv"] = [
        "/usr/bin/env",
        str(item.paths.project_root / "scripts/knowledge_index_incremental.sh"),
    ]
    cli.jobs = [collision]

    with pytest.raises(RuntimeError, match="Unknown cron job targets"):
        item._preflight_cron_inventory()


def _prepare_committed_verify_runtime(
    item: core.IntegrationManager,
    cli: StatefulCronCli,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    jobs = {
        "incremental": job_for_spec(
            item._incremental_spec(), job_id="committed-incremental", enabled=True,
        ),
        "snapshot": job_for_spec(
            item._snapshot_spec(), job_id="committed-snapshot", enabled=True,
        ),
        "gemini": gemini_job(item, job_id="committed-gemini", enabled=False),
        "unknown": customer_job(),
    }
    cli.jobs = json.loads(json.dumps(list(jobs.values())))
    hashes = item._inventory_hashes(cli.jobs)
    unknown = {"customer": hashes["customer"]}
    gemini = {"committed-gemini": hashes["committed-gemini"]}
    manifest = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "phase": "committed",
        "ownership": item._ownership_payload(),
        "indexState": "READY",
        "cronId": "committed-incremental",
        "snapshotCronId": "committed-snapshot",
        "initialIndexJobId": None,
        "managedCronIdsAfter": ["committed-incremental", "committed-snapshot"],
        "cronContractHashVersion": core.CRON_CONTRACT_HASH_VERSION,
        "cronUnknownHashesBefore": dict(unknown),
        "cronCommitUnknownHashes": dict(unknown),
        "cronPreservedGeminiHashesAfterQuiesce": dict(gemini),
        "cronCommitGeminiHashes": dict(gemini),
        "cronCommitTopologyVerified": True,
        "cronCommitInventoryHashes": dict(hashes),
    }
    item.store.write(manifest)
    monkeypatch.setattr(item, "_verify_local_source_map", lambda: None)
    monkeypatch.setattr(item, "_verify_runtime_contract_files", lambda: None)
    monkeypatch.setattr(item, "_verify_snapshot_wrapper_contract", lambda: None)
    monkeypatch.setattr(item, "_verify_plugin_skill_gateway", lambda: (True, True, True))
    monkeypatch.setattr(item, "_health_receipt_status", lambda: "ok")
    return manifest, jobs


@pytest.mark.parametrize("fault", ["removed", "reenabled", "schedule-drift"])
def test_verify_binds_committed_gemini_ids_and_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / fault, cli)
    _prepare_committed_verify_runtime(item, cli, monkeypatch)
    if fault == "removed":
        cli.jobs = [job for job in cli.jobs if job["id"] != "committed-gemini"]
    else:
        current = next(job for job in cli.jobs if job["id"] == "committed-gemini")
        if fault == "reenabled":
            current["enabled"] = True
        else:
            current["schedule"]["expr"] = "16 6 * * *"

    with pytest.raises(RuntimeError, match="Gemini cron inventory receipt drifted"):
        item.verify()


def test_verify_allows_later_unrelated_unknown_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    _prepare_committed_verify_runtime(item, cli, monkeypatch)
    next(job for job in cli.jobs if job["id"] == "customer")["description"] = (
        "Customer changed this after the Qwen commit."
    )

    assert item.verify()["ok"] is True


@pytest.mark.parametrize("fault", ["removed", "schedule-drift"])
def test_integrate_never_rebaselines_committed_gemini_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / fault, cli)
    _prepare_committed_verify_runtime(item, cli, monkeypatch)
    if fault == "removed":
        cli.jobs = [job for job in cli.jobs if job["id"] != "committed-gemini"]
    else:
        next(job for job in cli.jobs if job["id"] == "committed-gemini")["schedule"][
            "expr"
        ] = "17 6 * * *"
    monkeypatch.setattr(
        item, "begin",
        lambda **_: (_ for _ in ()).throw(AssertionError("drift must not be rebased")),
    )

    with pytest.raises(RuntimeError, match="Gemini cron inventory receipt drifted"):
        item._integrate_locked({"runtimePort": 18888})
    assert not any(call[:2] in (["cron", "rm"], ["cron", "edit"], ["cron", "add"])
                   for call in cli.calls)


def test_nonce_rollback_rejects_forged_target_receipt_without_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    unknown = customer_job()
    victim = customer_job(job_id="victim")
    victim["declarationKey"] = "victim-v1"
    cli.jobs = [unknown, victim]
    write_rollback_transaction(
        item, prior_definitions=[], unknown=unknown,
        target_ids=["victim"], managed_after=[],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)

    with pytest.raises(RuntimeError, match="receipt graph is inconsistent"):
        item._rollback_locked(require_exact_post_config=False)

    assert {job["id"] for job in cli.jobs} == {"customer", "victim"}
    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)


def test_nonce_rollback_rejects_reused_original_id_without_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    unknown = customer_job()
    original = core._job_definition(
        job_for_spec(item._incremental_spec(), job_id="old-managed", enabled=True)
    )
    hostile = customer_job(job_id="old-managed")
    hostile["declarationKey"] = "hostile-replacement-v1"
    cli.jobs = [unknown, hostile]
    write_rollback_transaction(
        item, prior_definitions=[original], unknown=unknown,
        target_ids=["old-managed"], managed_after=[],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)

    with pytest.raises(RuntimeError, match="identity was reused or drifted"):
        item._rollback_locked(require_exact_post_config=False)

    assert next(job for job in cli.jobs if job["id"] == "old-managed") == hostile
    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)


def test_nonce_rollback_rejects_reused_checkpointed_id_without_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    unknown = customer_job()
    write_rollback_transaction(
        item, prior_definitions=[], unknown=unknown, target_ids=[], managed_after=[],
    )
    transaction = item.store.read()
    item._ensure_cron_intent(
        transaction,
        bucket_name="cronStagingIntents",
        declaration_key=core.CRON_DECLARATION_KEY,
        canonical_description=core.INCREMENTAL_CRON_DESCRIPTION,
        role="managed",
        expected_factory=lambda description: item._managed_pre_alert_definition(
            item._incremental_spec(), description,
        ),
    )
    item._checkpoint_cron_intent_job_id(
        transaction,
        bucket_name="cronStagingIntents",
        declaration_key=core.CRON_DECLARATION_KEY,
        job_id="staged-id",
        created=True,
    )
    hostile = customer_job(job_id="staged-id")
    hostile["declarationKey"] = "hostile-staged-replacement-v1"
    cli.jobs = [unknown, hostile]
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)

    with pytest.raises(RuntimeError, match="checkpointed cron identity was reused or drifted"):
        item._rollback_locked(require_exact_post_config=False)

    assert next(job for job in cli.jobs if job["id"] == "staged-id") == hostile
    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)


def test_integrate_rechecks_committed_gemini_after_late_verify_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DriftOnSecondInventoryCli(StatefulCronCli):
        inventory_reads = 0

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:4] == ["cron", "list", "--all", "--json"]:
                self.inventory_reads += 1
                if self.inventory_reads == 2:
                    gemini = next(
                        job for job in self.jobs
                        if job["id"] == "committed-gemini"
                    )
                    gemini["schedule"]["expr"] = "18 6 * * *"
            return super().json(args, timeout=timeout)

    cli = DriftOnSecondInventoryCli()
    item = manager(tmp_path, cli)
    _prepare_committed_verify_runtime(item, cli, monkeypatch)
    monkeypatch.setattr(
        item,
        "_verify_runtime_contract_files",
        lambda: (_ for _ in ()).throw(RuntimeError("late runtime verify fault")),
    )
    monkeypatch.setattr(
        item,
        "begin",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("Gemini drift must fail before begin")
        ),
    )

    with pytest.raises(RuntimeError, match="Gemini cron inventory receipt drifted"):
        item._integrate_locked({"runtimePort": 18888})

    assert cli.inventory_reads == 2
    assert not any(
        call[:2] in (["cron", "add"], ["cron", "edit"], ["cron", "rm"])
        for call in cli.calls
    )


def test_recurring_activation_snapshot_guard_rejects_same_id_drift_before_enable(
    tmp_path: Path,
) -> None:
    class DriftBeforeEditCli(StatefulCronCli):
        inventory_reads = 0

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:4] == ["cron", "list", "--all", "--json"]:
                self.inventory_reads += 1
                if self.inventory_reads == 2:
                    target = next(job for job in self.jobs if job["id"] == "incremental")
                    target["description"] = "Concurrent same-ID drift."
            return super().json(args, timeout=timeout)

    cli = DriftBeforeEditCli()
    item = manager(tmp_path, cli)
    cli.jobs = [
        job_for_spec(item._incremental_spec(), job_id="incremental", enabled=False),
        job_for_spec(item._snapshot_spec(), job_id="snapshot", enabled=False),
    ]

    with pytest.raises(RuntimeError, match="inventory changed before edit"):
        item._enable_recurring_jobs(["incremental", "snapshot"])

    assert cli.inventory_reads == 2
    assert not any(
        call[:2] == ["cron", "edit"] and "--enable" in call
        for call in cli.calls
    )
    assert all(job["enabled"] is False for job in cli.jobs)


def legacy_committed_v3_gemini_fixture(
    item: core.IntegrationManager,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    prior_gemini = gemini_job(item, job_id="legacy-gemini", enabled=True)
    current_gemini = json.loads(json.dumps(prior_gemini))
    current_gemini["enabled"] = False
    incremental = job_for_spec(
        item._incremental_spec(), job_id="current-incremental", enabled=True,
    )
    snapshot = job_for_spec(
        item._snapshot_spec(), job_id="current-snapshot", enabled=True,
    )
    prior_gemini_hash = core._legacy_v3_job_contract_hash(
        prior_gemini, include_id=True,
    )
    transaction = {
        "schemaVersion": 1,
        "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
        "phase": "committed",
        "ownership": item._ownership_payload(),
        "indexState": "READY",
        "cronId": "current-incremental",
        "snapshotCronId": "current-snapshot",
        "initialIndexJobId": None,
        "cronDefinitionsBefore": [prior_gemini],
        "cronTargetIdsBefore": ["legacy-gemini"],
        "cronInventoryHashesBefore": {"legacy-gemini": prior_gemini_hash},
        "cronUnknownHashesBefore": {},
        "disabledGeminiJobs": [{"id": "legacy-gemini", "wasEnabled": True}],
    }
    return transaction, [incremental, snapshot, current_gemini]


def test_legacy_committed_v3_repair_authority_accepts_exact_gemini_receipt(
    tmp_path: Path,
) -> None:
    item = manager(tmp_path)
    transaction, jobs = legacy_committed_v3_gemini_fixture(item)

    item._verify_legacy_committed_v3_repair_authority(transaction, jobs)


@pytest.mark.parametrize("fault", ["missing", "schedule-drift"])
def test_legacy_committed_v3_repair_authority_rejects_gemini_drift(
    tmp_path: Path, fault: str,
) -> None:
    item = manager(tmp_path / fault)
    transaction, jobs = legacy_committed_v3_gemini_fixture(item)
    if fault == "missing":
        jobs = [job for job in jobs if job["id"] != "legacy-gemini"]
    else:
        next(job for job in jobs if job["id"] == "legacy-gemini")["schedule"][
            "expr"
        ] = "19 6 * * *"

    with pytest.raises(RuntimeError, match="Legacy committed v3 Gemini contract drifted"):
        item._verify_legacy_committed_v3_repair_authority(transaction, jobs)


def _valid_announce_alert(item: core.IntegrationManager) -> dict[str, Any]:
    return {
        "after": 1,
        "cooldownMs": 3600000,
        "includeSkipped": False,
        "mode": "announce",
        "channel": item.report_channel,
        "to": item.report_to,
        "accountId": item.report_account_id,
    }


@pytest.mark.parametrize(
    ("case", "mutation"),
    [
        ("name", lambda job, item: job.update(name="-unsafe-name")),
        ("description", lambda job, item: job.update(description="-unsafe-description")),
        ("cron-option", lambda job, item: job["schedule"].update(expr="--help")),
        (
            "cron-invalid",
            lambda job, item: job["schedule"].update(expr="not a cron expression"),
        ),
        ("timezone", lambda job, item: job["schedule"].update(tz="--help")),
        (
            "delivery-channel",
            lambda job, item: job.update(delivery={
                "mode": "announce", "channel": "--help", "to": item.report_to,
            }),
        ),
        (
            "delivery-to",
            lambda job, item: job.update(delivery={
                "mode": "announce", "channel": item.report_channel, "to": "--help",
            }),
        ),
        (
            "alert-channel",
            lambda job, item: job.update(failureAlert={
                **_valid_announce_alert(item), "channel": "--help",
            }),
        ),
        (
            "alert-to",
            lambda job, item: job.update(failureAlert={
                **_valid_announce_alert(item), "to": "--help",
            }),
        ),
        (
            "env-value",
            lambda job, item: job["payload"].update(env={"SAFE_VALUE": "--help"}),
        ),
    ],
)
def test_cli_option_like_or_invalid_contract_fails_before_begin(
    tmp_path: Path, case: str, mutation: Any,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / case, cli)
    unsafe = gemini_job(item)
    mutation(unsafe, item)
    cli.jobs = [unsafe]

    with pytest.raises(RuntimeError, match="safe upgrade allowlist"):
        item._integrate_locked({"runtimePort": 18888})

    assert not item.store.manifest_path.exists()
    assert cli.calls == [["cron", "list", "--all", "--json"]]


def test_valid_cli_option_values_restore_to_exact_disabled_contract(
    tmp_path: Path,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    original = job_for_spec(
        item._incremental_spec(), job_id="prior-incremental", enabled=True,
    )
    original["delivery"] = {
        "mode": "announce",
        "channel": item.report_channel,
        "to": item.report_to,
        "accountId": item.report_account_id,
    }
    definition = core._job_definition(original)
    transaction = write_staging_transaction(item, [], set())

    restored_id = item._restore_cron_definition(definition, transaction)

    restored = next(job for job in cli.jobs if job["id"] == restored_id)
    expected = json.loads(json.dumps(definition))
    expected["id"] = restored_id
    expected["enabled"] = False
    assert core._job_definition(restored) == expected
    assert any(
        call[:2] == ["cron", "add"]
        and call[call.index("--cron") + 1] == original["schedule"]["expr"]
        and call[call.index("--tz") + 1] == original["schedule"]["tz"]
        and call[call.index("--channel") + 1] == item.report_channel
        and call[call.index("--to") + 1] == item.report_to
        for call in cli.calls
    )


def test_quiescence_waits_until_live_running_at_ms_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningAtMsCli(StatefulCronCli):
        inventory_reads = 0

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:4] == ["cron", "list", "--all", "--json"]:
                self.inventory_reads += 1
                if self.inventory_reads == 3:
                    self.jobs[0]["state"].pop("runningAtMs")
            return super().json(args, timeout=timeout)

    cli = RunningAtMsCli()
    item = manager(tmp_path, cli)
    active = job_for_spec(
        item._incremental_spec(), job_id="incremental", enabled=False,
    )
    active["state"] = {"runningAtMs": 1788746400000}
    cli.jobs = [active]
    monkeypatch.setattr(core.time, "sleep", lambda _: None)

    item._wait_for_quiesced_jobs(
        {"incremental"}, timeout_seconds=1, poll_seconds=0.01,
    )

    assert cli.inventory_reads == 3
    assert "runningAtMs" not in cli.jobs[0]["state"]


def test_quiescence_times_out_while_live_running_at_ms_remains(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    active = job_for_spec(
        item._incremental_spec(), job_id="incremental", enabled=False,
    )
    active["state"] = {"runningAtMs": 1788746400000}
    cli.jobs = [active]

    with pytest.raises(RuntimeError, match="did not quiesce"):
        item._wait_for_quiesced_jobs({"incremental"}, timeout_seconds=0)

    assert not any(call[:2] == ["cron", "edit"] for call in cli.calls)


@pytest.mark.parametrize("running_at_ms", [True, 0, "1788746400000"])
def test_quiescence_rejects_malformed_live_running_at_ms(
    tmp_path: Path, running_at_ms: Any,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    malformed = job_for_spec(
        item._incremental_spec(), job_id="incremental", enabled=False,
    )
    malformed["state"] = {"runningAtMs": running_at_ms}
    cli.jobs = [malformed]

    with pytest.raises(RuntimeError, match="running timestamp"):
        item._wait_for_quiesced_jobs({"incremental"}, timeout_seconds=0)

    assert cli.calls == [["cron", "list", "--all", "--json"]]


@pytest.mark.parametrize("fault", ["mutated-unknown", "extra-job"])
def test_managed_add_inventory_guard_stops_before_checkpoint_or_edit(
    tmp_path: Path, fault: str,
) -> None:
    class HostileAddCli(StatefulCronCli):
        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            result = super().json(args, timeout=timeout)
            if args[:2] == ["cron", "add"]:
                if fault == "mutated-unknown":
                    unknown = next(job for job in self.jobs if job["id"] == "customer")
                    unknown["description"] = "Concurrent unknown mutation."
                else:
                    injected = customer_job(job_id="concurrent-extra")
                    injected["declarationKey"] = "concurrent-extra-v1"
                    self.jobs.append(injected)
            return result

    cli = HostileAddCli()
    item = manager(tmp_path / fault, cli)
    unknown = customer_job()
    cli.jobs = [json.loads(json.dumps(unknown))]
    transaction = write_staging_transaction(item, [unknown], set())
    spec = item._incremental_spec()

    with pytest.raises(RuntimeError, match="changed more than its exact staged candidate"):
        item._apply_managed_spec(spec, transaction)

    persisted = item.store.read()
    intent = persisted["cronStagingIntents"][spec.key]
    assert "jobId" not in intent
    assert persisted["managedCronIdsAfter"] == []
    assert not any(call[:2] == ["cron", "edit"] for call in cli.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled-missing", None),
        ("enabled-none", None),
        ("enabled-string", "false"),
        ("delete-after-run-int", 0),
    ],
)
def test_managed_contract_rejects_non_boolean_state_fields(
    tmp_path: Path, field: str, value: Any,
) -> None:
    item = manager(tmp_path / field)
    job = job_for_spec(item._incremental_spec(), job_id="managed", enabled=False)
    if field == "enabled-missing":
        job.pop("enabled")
    elif field.startswith("enabled-"):
        job["enabled"] = value
    else:
        job["deleteAfterRun"] = value

    assert not core._job_matches_spec(
        job, item._incremental_spec(), require_enabled=False,
    )
    assert not core._job_matches_spec(
        job, item._incremental_spec(), require_enabled=True,
    )
    with pytest.raises(RuntimeError):
        core._job_definition(job)


@pytest.mark.parametrize("enabled", [0, 1])
def test_legacy_snapshot_rejects_integer_enabled_state(
    tmp_path: Path, enabled: int,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path / str(enabled), cli)
    legacy = legacy_snapshot_job(item)
    legacy["enabled"] = enabled
    cli.jobs = [legacy]

    assert not item._legacy_snapshot_job_matches(legacy, require_known_key=True)
    with pytest.raises(RuntimeError):
        core._job_definition(legacy)
    with pytest.raises(RuntimeError, match="exact migration allowlist"):
        item._preflight_cron_inventory()


def test_rollback_waits_for_running_disabled_removal_target_before_rm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    original = job_for_spec(
        item._incremental_spec(), job_id="prior-incremental", enabled=True,
    )
    disabled_running = json.loads(json.dumps(original))
    disabled_running["enabled"] = False
    disabled_running["state"] = {"runningAtMs": 1788746400000}
    unknown = customer_job()
    cli.jobs = [disabled_running, unknown]
    write_rollback_transaction(
        item,
        prior_definitions=[core._job_definition(original)],
        unknown=unknown,
        target_ids=["prior-incremental"],
        managed_after=[],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monotonic_calls = 0

    def expired_monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 0.0 if monotonic_calls == 1 else 2000.0

    monkeypatch.setattr(core.time, "monotonic", expired_monotonic)

    with pytest.raises(RuntimeError, match="did not quiesce"):
        item._rollback_locked(require_exact_post_config=False)

    assert monotonic_calls == 2
    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)
    assert any(job["id"] == "prior-incremental" for job in cli.jobs)


@pytest.mark.parametrize(
    ("late_state", "message"),
    [
        ({"runningAtMs": 1788746400000}, "became active"),
        ("malformed-runtime-state", "runtime state is malformed"),
    ],
)
def test_exact_cron_removal_rechecks_late_runtime_state_before_every_rm(
    tmp_path: Path, late_state: Any, message: str,
) -> None:
    class LateRuntimeStateCli(StatefulCronCli):
        inventory_reads = 0

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:4] == ["cron", "list", "--all", "--json"]:
                self.inventory_reads += 1
                if self.inventory_reads == 2:
                    target = next(job for job in self.jobs if job["id"] == "managed")
                    target["state"] = late_state
            return super().json(args, timeout=timeout)

    cli = LateRuntimeStateCli()
    item = manager(tmp_path, cli)
    managed = job_for_spec(
        item._incremental_spec(), job_id="managed", enabled=False,
    )
    cli.jobs = [json.loads(json.dumps(managed))]

    with pytest.raises(RuntimeError, match=message):
        item._remove_cron_ids_with_snapshot_guard(
            [json.loads(json.dumps(managed))], {"managed"},
        )

    assert cli.inventory_reads == 2
    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)
    assert any(job["id"] == "managed" for job in cli.jobs)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("null-state", "runtime state is malformed"),
        ("null-running-at", "running timestamp is malformed"),
        ("null-state-status", "runtime status is malformed"),
        ("null-top-status", "runtime status is malformed"),
    ],
)
def test_exact_cron_removal_rejects_explicit_null_runtime_markers(
    tmp_path: Path, fault: str, message: str,
) -> None:
    class NullRuntimeMarkerCli(StatefulCronCli):
        inventory_reads = 0

        def json(self, args: list[str], *, timeout: int = 120) -> Any:
            if args[:4] == ["cron", "list", "--all", "--json"]:
                self.inventory_reads += 1
                if self.inventory_reads == 2:
                    target = next(job for job in self.jobs if job["id"] == "managed")
                    if fault == "null-state":
                        target["state"] = None
                    elif fault == "null-running-at":
                        target["state"] = {"runningAtMs": None}
                    elif fault == "null-state-status":
                        target["state"] = {"status": None}
                    else:
                        target["status"] = None
            return super().json(args, timeout=timeout)

    cli = NullRuntimeMarkerCli()
    item = manager(tmp_path / fault, cli)
    managed = job_for_spec(
        item._incremental_spec(), job_id="managed", enabled=False,
    )
    cli.jobs = [json.loads(json.dumps(managed))]

    with pytest.raises(RuntimeError, match=message):
        item._remove_cron_ids_with_snapshot_guard(
            [json.loads(json.dumps(managed))], {"managed"},
        )

    assert cli.inventory_reads == 2
    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)
    assert any(job["id"] == "managed" for job in cli.jobs)


def test_exact_cron_removal_rejects_enabled_target_before_rm(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    managed = job_for_spec(
        item._incremental_spec(), job_id="managed", enabled=True,
    )
    cli.jobs = [json.loads(json.dumps(managed))]

    with pytest.raises(RuntimeError, match="not disabled"):
        item._remove_cron_ids_with_snapshot_guard(
            [json.loads(json.dumps(managed))], {"managed"},
        )

    assert not any(call[:2] == ["cron", "rm"] for call in cli.calls)
    assert any(job["id"] == "managed" for job in cli.jobs)


def test_integrate_rollback_failure_preserves_checkpointed_restore_receipt_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailOnceAfterRestoreCheckpointCli(StatefulCronCli):
        item: core.IntegrationManager | None = None
        failed = False

        def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
            if args[:2] == ["cron", "edit"] and not self.failed:
                job = next(entry for entry in self.jobs if entry["id"] == args[2])
                if str(job.get("name", "")).startswith("qwen-restore-stage-"):
                    assert self.item is not None
                    receipt = self.item.store.read()
                    intent = receipt["cronRestoreIntents"][job["declarationKey"]]
                    assert intent["jobId"] == args[2]
                    assert receipt["restoredCronIdsByDeclaration"][
                        job["declarationKey"]
                    ] == args[2]
                    self.failed = True
                    raise RuntimeError("rollback failed after durable restore checkpoint")
            return super().run(args, timeout=timeout, check=check)

    cli = FailOnceAfterRestoreCheckpointCli()
    item = manager(tmp_path, cli)
    cli.item = item
    prior_incremental = job_for_spec(
        item._incremental_spec(), job_id="prior-incremental", enabled=True,
    )
    prior_snapshot = job_for_spec(
        item._snapshot_spec(), job_id="prior-snapshot", enabled=True,
    )
    unknown = customer_job()
    prior_definitions = [
        core._job_definition(prior_incremental),
        core._job_definition(prior_snapshot),
    ]
    cli.jobs = json.loads(json.dumps([prior_incremental, prior_snapshot, unknown]))
    write_rollback_transaction(
        item,
        prior_definitions=prior_definitions,
        unknown=unknown,
        target_ids=["prior-incremental", "prior-snapshot"],
        managed_after=[],
    )
    begin_receipt = item.store.read()
    begin_receipt["phase"] = "prepared"
    begin_receipt["cronMutationStarted"] = False
    begin_receipt["projectExisted"] = True
    item.store.write({
        "schemaVersion": 1,
        "contractVersion": 1,
        "runId": "prior-install",
        "phase": "committed",
        "ownership": {"schema": "qwen-local-openclaw.v1"},
    })
    prepare_collision_integration_runtime(item, monkeypatch, run_id="rollback-retry")
    monkeypatch.setattr(
        item, "begin", lambda **_: json.loads(json.dumps(begin_receipt)),
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)
    monkeypatch.setattr(item, "_checkpoint_mutation", lambda *_: None)
    monkeypatch.setattr(
        item,
        "_verify_recurring_specs",
        lambda **_: (_ for _ in ()).throw(RuntimeError("primary integration fault")),
    )

    with pytest.raises(core.IntegrationRollbackIncomplete) as caught:
        item._integrate_locked({"runtimePort": 18888})

    assert "primary integration fault" in str(caught.value.original_error)
    assert "durable restore checkpoint" in str(caught.value.rollback_error)
    failed_receipt = item.store.read()
    assert failed_receipt["phase"] == "rollback_failed"
    checkpointed = next(
        intent for intent in failed_receipt["cronRestoreIntents"].values()
        if "jobId" in intent
    )
    checkpointed_id = checkpointed["jobId"]
    assert checkpointed_id in failed_receipt["restoredCronIdsByDeclaration"].values()

    result = item._rollback_locked(require_exact_post_config=False)

    assert result["status"] == "ROLLED_BACK"
    assert item.store.read()["phase"] == "rolled_back"
    assert sum(call[:2] == ["cron", "add"] for call in cli.calls) == 4
    restored = [job for job in cli.jobs if job["id"] != "customer"]
    assert sorted(core._job_contract_hash(job) for job in restored) == sorted(
        core._job_contract_hash(definition) for definition in prior_definitions
    )


def test_first_prepared_receipt_survives_write_crash_and_can_restart_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    unknown = customer_job()
    cli.jobs = [unknown]
    inventory_hashes = item._inventory_hashes(cli.jobs)
    monkeypatch.setattr(item, "preflight", lambda: {})
    monkeypatch.setattr(item, "_config_file", lambda: config)
    real_write = item.store.write
    write_calls = 0

    def crash_after_first_durable_write(payload: dict[str, Any]) -> Path:
        nonlocal write_calls
        write_calls += 1
        path = real_write(payload)
        if write_calls == 1:
            raise SimulatedCrash("process stopped after first durable receipt")
        return path

    monkeypatch.setattr(item.store, "write", crash_after_first_durable_write)

    with pytest.raises(SimulatedCrash):
        item.begin(cron_inventory_hashes=inventory_hashes)

    monkeypatch.setattr(item.store, "write", real_write)
    prepared = item.store.read()
    assert prepared["phase"] == "prepared"
    assert prepared["ownership"] == item._ownership_payload()
    assert prepared["cronMutationStarted"] is False
    assert prepared["runtimeMutationStarted"] is False
    assert prepared["cronInventoryHashesBefore"] == inventory_hashes
    assert prepared["cronUnknownHashesBefore"] == inventory_hashes

    rolled_back = item._rollback_locked(require_exact_post_config=False)
    restarted = item.begin(cron_inventory_hashes=inventory_hashes)

    assert rolled_back["status"] == "ROLLED_BACK"
    assert restarted["phase"] == "prepared"
    assert restarted["runId"] != prepared["runId"]
    assert restarted["cronInventoryHashesBefore"] == inventory_hashes
