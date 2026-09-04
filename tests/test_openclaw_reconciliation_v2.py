from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

import src.openclaw_integration.core as core


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


class StatefulCronCli:
    def __init__(self) -> None:
        self.executable = str(Path(sys.executable).resolve())
        self.jobs: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []

    def json(self, args: list[str], *, timeout: int = 120) -> Any:
        self.calls.append(list(args))
        if args[:4] == ["cron", "list", "--all", "--json"]:
            return cron_payload(self.jobs)
        if args[:2] == ["cron", "add"]:
            key = args[args.index("--declaration-key") + 1]
            existing = next((job for job in self.jobs if job.get("declarationKey") == key), None)
            job_id = str(existing["id"]) if existing else f"job-{len(self.jobs) + 1}"
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
                "deleteAfterRun": "--delete-after-run" in args,
                "schedule": schedule,
                "payload": payload,
                "delivery": delivery,
            }
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
            self.jobs = [job for job in self.jobs if str(job["id"]) != args[2]]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["cron", "disable"]:
            job = next(item for item in self.jobs if item["id"] == args[2])
            job["enabled"] = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["cron", "edit"]:
            job = next(item for item in self.jobs if item["id"] == args[2])
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
    with pytest.raises(RuntimeError, match="count"):
        core._cron_jobs(cron_payload([one], total=2))
    with pytest.raises(RuntimeError, match="duplicate"):
        core._cron_jobs(cron_payload([one, {**two, "id": "one"}]))
    with pytest.raises(RuntimeError, match="duplicate"):
        core._cron_jobs(cron_payload([one, {**two, "declarationKey": "key-one"}]))

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
        "contractVersion": 2,
        "runId": "gemini-target",
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "projectExisted": True,
        "healthReceiptExisted": False,
    }
    captured: set[str] = set()
    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([gemini], []))
    monkeypatch.setattr(item, "begin", lambda: dict(base))

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


def test_runtime_quiescence_guard_refuses_held_index_lock_without_mutation(tmp_path: Path) -> None:
    item = manager(tmp_path)
    data = item.paths.project_root / "data"
    data.mkdir(mode=0o700)
    index_lock = data / "index.lock"
    index_lock.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="index run did not quiesce"):
        with item._runtime_quiescence_guard(timeout_seconds=0, poll_seconds=0.01):
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
            with item._runtime_quiescence_guard(timeout_seconds=0, poll_seconds=0.01):
                pytest.fail("guard must not yield while the snapshot lock is held")
    finally:
        core.fcntl.flock(descriptor, core.fcntl.LOCK_UN)
        os.close(descriptor)

    assert snapshot_lock.is_file()
    assert not (item.paths.project_root / "data/index.lock").exists()


def test_managed_contract_requires_exact_alert_delivery_env_and_tools(tmp_path: Path) -> None:
    item = manager(tmp_path)
    spec = item._incremental_spec()
    expected = job_for_spec(spec, job_id="inc", enabled=True)
    assert core._job_matches_spec(expected, spec, require_enabled=True)

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

    def begin() -> dict[str, Any]:
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


def test_both_recurring_jobs_are_verified_disabled_before_global_enable(tmp_path: Path) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)

    incremental_id = item._apply_managed_spec(item._incremental_spec())
    snapshot_id = item._apply_managed_spec(item._snapshot_spec())

    assert all(job["enabled"] is False for job in cli.jobs)
    item._verify_recurring_specs(enabled=False)
    item._enable_recurring_jobs([incremental_id, snapshot_id])
    assert all(job["enabled"] is True for job in cli.jobs)
    assert cli.calls[0] and ["cron", "list", "--all", "--json"] in cli.calls


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
    def guard() -> Iterator[dict[str, Any]]:
        yield {"snapshotLockCreated": False, "persisted": False}

    monkeypatch.setattr(item, "begin", lambda: dict(base))
    monkeypatch.setattr(item, "_prepare_snapshot_root", lambda: True)
    monkeypatch.setattr(item, "_runtime_quiescence_guard", guard)
    monkeypatch.setattr(item, "bootstrap_project", lambda _: False)
    monkeypatch.setattr(item, "synchronize_project_runtime", lambda: None)
    monkeypatch.setattr(item, "_allowed_projects", lambda: [])
    monkeypatch.setattr(item, "configure_openclaw", lambda _allowed, **_: None)
    monkeypatch.setattr(item, "install_launchd_plist", lambda _: None)
    monkeypatch.setattr(item, "activate_launchd", lambda: None)
    monkeypatch.setattr(item, "mark_ready_or_schedule_build", lambda: ("READY", None))
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
    monkeypatch.setattr(item, "begin", lambda: dict(base_transaction))
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
        "schemaVersion": 1, "contractVersion": 2, "runId": "fault", "phase": "prepared",
        "ownedAssets": [], "configPath": str(config), "healthReceiptExisted": False,
        "projectExisted": True,
    }

    @contextmanager
    def guard() -> Iterator[dict[str, Any]]:
        yield {"snapshotLockCreated": False, "persisted": False}

    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "begin", lambda: dict(base))
    monkeypatch.setattr(item, "_prepare_snapshot_root", lambda: True)
    monkeypatch.setattr(item, "_runtime_quiescence_guard", guard)
    monkeypatch.setattr(item, "bootstrap_project", lambda _: False)
    monkeypatch.setattr(item, "synchronize_project_runtime", lambda: events.append("sync"))
    monkeypatch.setattr(item, "_allowed_projects", lambda: [])
    monkeypatch.setattr(item, "configure_openclaw", lambda _allowed, **_: events.append("configure"))
    monkeypatch.setattr(item, "install_launchd_plist", lambda _: events.append("plist"))
    monkeypatch.setattr(item, "activate_launchd", lambda: events.append("launchd"))
    staged = iter(["incremental", "snapshot"])
    monkeypatch.setattr(item, "_apply_managed_spec", lambda _: next(staged))
    monkeypatch.setattr(item, "_verify_recurring_specs", lambda **_: events.append("global-disabled"))
    monkeypatch.setattr(item, "disable_owned_gemini_jobs", lambda: [])
    monkeypatch.setattr(item, "mark_ready_or_schedule_build", lambda: ("READY", None))
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
        "contractVersion": 2,
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
    monkeypatch.setattr(item, "begin", lambda: dict(base))
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
        "contractVersion": 2,
        "runId": "rollback-incomplete",
        "phase": "prepared",
        "ownedAssets": [],
        "configPath": str(config),
        "projectExisted": True,
        "healthReceiptExisted": False,
    }
    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "begin", lambda: dict(base))
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
        "contractVersion": 2,
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
        "cronDefinitionsBefore": prior_definitions,
        "cronUnknownHashesBefore": {
            str(unknown["id"]): core._job_contract_hash(unknown, include_id=True),
        },
        "cronInventoryHashesBefore": {},
        "cronTargetIdsBefore": target_ids,
        "managedCronIdsAfter": managed_after,
        "snapshotRootCreated": False,
        "snapshotLockCreated": False,
    })


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
        managed_after=["incremental"],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    result = item._rollback_locked(require_exact_post_config=False)

    assert result == {"ok": True, "status": "ROLLED_BACK"}
    assert item.store.read()["phase"] == "rolled_back"
    after_unknown = next(job for job in cli.jobs if job["id"] == "customer")
    assert core._job_contract_hash(after_unknown, include_id=True) == core._job_contract_hash(
        unknown, include_id=True,
    )
    restored = [job for job in cli.jobs if job["id"] != "customer"]
    assert sorted(core._job_contract_hash(job) for job in restored) == sorted(
        core._job_contract_hash(definition) for definition in prior
    )


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
        managed_after=["incremental"],
    )
    monkeypatch.setattr(item, "_verify_config_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(item, "_remove_created_snapshot_artifacts", lambda _: None)

    with pytest.raises(subprocess.CalledProcessError):
        item._rollback_locked(require_exact_post_config=False)

    assert item.store.read()["phase"] == "failed"


def test_failed_fresh_install_removes_only_recorded_empty_snapshot_root_and_lock(tmp_path: Path) -> None:
    item = manager(tmp_path)
    item.snapshot_root.mkdir(parents=True, mode=0o700)
    lock = item.snapshot_root / ".snapshot-run.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    metadata = lock.stat()

    item._remove_created_snapshot_artifacts({
        "snapshotLockCreated": True,
        "snapshotLockDev": metadata.st_dev,
        "snapshotLockIno": metadata.st_ino,
        "snapshotRootCreated": True,
    })

    assert not item.snapshot_root.exists()


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
        "schedule": {"kind": "at", "at": "2026-09-04T07:00:00+08:00"},
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
    cli = RestoreRecordingCli()
    item = manager(tmp_path, cli)
    definition = initial_job(item, enabled=True)
    receipt = core._job_definition(definition)

    assert receipt["deleteAfterRun"] is True
    assert item._restore_cron_definition(receipt) == "restored"
    add = cli.json_calls[0]
    assert add[add.index("--at") + 1] == definition["schedule"]["at"]
    assert "--delete-after-run" in add and "--disabled" in add and "--no-deliver" in add
    alert = cli.run_calls[0]
    assert alert[alert.index("--failure-alert-mode") + 1] == "announce"
    assert "--failure-alert-exclude-skipped" in alert
    assert cli.run_calls[-1] == ["cron", "edit", "restored", "--enable"]


@pytest.mark.parametrize("enabled", [True, False])
def test_cron_rollback_round_trips_legacy_definition_exactly(
    tmp_path: Path, enabled: bool,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    original = legacy_snapshot_job(item, enabled=enabled)
    definition = core._job_definition(original)

    restored_id = item._restore_cron_definition(definition)

    restored = next(job for job in cli.jobs if job["id"] == restored_id)
    assert core._job_contract_hash(restored) == core._job_contract_hash(definition)
    add = next(call for call in cli.calls if call[:2] == ["cron", "add"])
    assert "--disabled" in add
    assert add[add.index("--declaration-key") + 1] == core.LEGACY_SNAPSHOT_DECLARATION_KEY
    assert (any(call[:3] == ["cron", "edit", restored_id] and "--enable" in call
                for call in cli.calls)) is enabled


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
    cron_mutation_started: bool = False,
    **markers: bool,
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

    item._restore_plugin_from_snapshot(
        receipt, snapshot_path=snapshot_dir / "openclaw-config.preinstall",
    )

    assert (item.plugin_target / "index.js").read_text(encoding="utf-8") == "old"
    assert (item.plugin_target / "dist/runtime.js").read_text(encoding="utf-8") == "old-runtime"
    restored_link = item.plugin_target / "node_modules/openclaw"
    assert restored_link.is_symlink()
    assert os.readlink(restored_link) == str(openclaw_package)
    assert not (item.plugin_target / "added.js").exists()
    assert item._safe_tree_sha256(item.plugin_target, label="restored plugin") == receipt[
        "pluginBackupSha256"
    ]
    assert ["plugins", "uninstall", core.PLUGIN_ID, "--force"] in cli.calls


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
    (snapshot_dir / "plugin.preinstall/index.js").write_text("tampered", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        item._restore_plugin_from_snapshot(
            receipt, snapshot_path=snapshot_dir / "openclaw-config.preinstall",
        )

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
        "cronTargetIdsBefore": ["managed"],
        "managedCronIdsAfter": ["managed"],
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
        "schemaVersion": 1, "contractVersion": 2, "runId": "success", "phase": "prepared",
        "ownedAssets": [], "configPath": str(config), "healthReceiptExisted": False,
        "projectExisted": True,
    }
    events: list[str] = []
    real_write = item.store.write

    def record_write(payload: dict[str, Any]) -> Path:
        events.append(f"write:{payload['phase']}")
        return real_write(payload)

    monkeypatch.setattr(item.store, "write", record_write)
    monkeypatch.setattr(item, "begin", lambda: dict(base))
    monkeypatch.setattr(item, "_preflight_cron_inventory", lambda: ([], []))
    monkeypatch.setattr(item, "_prepare_snapshot_root", lambda: True)

    @contextmanager
    def guard() -> Iterator[dict[str, Any]]:
        yield {"snapshotLockCreated": False, "persisted": False}

    monkeypatch.setattr(item, "_runtime_quiescence_guard", guard)
    monkeypatch.setattr(item, "bootstrap_project", lambda _: False)
    monkeypatch.setattr(item, "synchronize_project_runtime", lambda: events.append("sync"))
    monkeypatch.setattr(item, "_allowed_projects", lambda: [])
    monkeypatch.setattr(item, "configure_openclaw", lambda _allowed, **_: events.append("configure"))
    monkeypatch.setattr(item, "install_launchd_plist", lambda _: events.append("plist"))
    monkeypatch.setattr(item, "activate_launchd", lambda: events.append("launchd"))
    monkeypatch.setattr(item, "_apply_managed_spec", lambda spec: f"job:{spec.key}")
    monkeypatch.setattr(item, "_verify_recurring_specs", lambda **_: events.append("disabled-verified"))
    monkeypatch.setattr(item, "disable_owned_gemini_jobs", lambda: [])
    monkeypatch.setattr(item, "mark_ready_or_schedule_build", lambda: ("READY", None))
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
    monkeypatch.setattr(item, "verify", lambda: verify_calls.append("verify") or {"ok": True})

    result = item._integrate_locked({"runtimePort": 18888})
    assert result["transaction"] == "committed"
    assert events.index("activation-verified") < events.index("write:committed")
    mutation_count = len(events)

    again = item._integrate_locked({"runtimePort": 18888})
    assert again["transaction"] == "already_current"
    assert len(events) == mutation_count
    assert len(verify_calls) == 2
