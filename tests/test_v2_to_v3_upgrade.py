from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

import src.openclaw_integration.core as core
from tests.test_openclaw_reconciliation_v2 import (
    StatefulCronCli,
    create_snapshot_root,
    job_for_spec,
    manager,
)


def _exact_v2_receipt(item: core.IntegrationManager) -> dict[str, Any]:
    ownership = item._ownership_payload()
    ownership.update({
        "schema": "qwen-local-openclaw.v2",
        "contractVersion": 2,
    })
    return {
        "schemaVersion": core.SCHEMA_VERSION,
        "contractVersion": 2,
        "runId": "committed-v2-install",
        "phase": "committed",
        "ownership": ownership,
        "indexState": "READY",
    }


def _unknown_customer_job(item: core.IntegrationManager) -> dict[str, Any]:
    job = job_for_spec(
        item._incremental_spec(), job_id="customer-unknown", enabled=True,
    )
    job.update({
        "name": "Customer-owned unrelated job",
        "description": "Must remain byte-for-byte stable across installer activity",
        "declarationKey": "customer.unmanaged.backup",
    })
    job["payload"] = {
        **job["payload"],
        "argv": ["/usr/bin/true"],
        "cwd": str(item.paths.workspace),
    }
    return job


def _seed_v2_install(
    item: core.IntegrationManager, cli: StatefulCronCli,
) -> list[dict[str, Any]]:
    jobs = [
        job_for_spec(
            item._incremental_spec(), job_id="v2-incremental", enabled=True,
        ),
        job_for_spec(
            item._snapshot_spec(), job_id="v2-snapshot", enabled=True,
        ),
        _unknown_customer_job(item),
    ]
    cli.jobs = copy.deepcopy(jobs)
    item.store.write(_exact_v2_receipt(item))
    return jobs


def _prepare_upgrade_runtime(
    item: core.IntegrationManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_activation: bool = False,
) -> Path:
    config = item.paths.home / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"fixture":"v2"}\n', encoding="utf-8")
    config.chmod(0o600)
    create_snapshot_root(item)

    @contextmanager
    def guard(*, checkpoint=None) -> Iterator[dict[str, Any]]:
        del checkpoint
        yield {"snapshotLockCreated": False, "persisted": False}

    monkeypatch.setattr(item, "preflight", lambda: None)
    monkeypatch.setattr(item, "_config_file", lambda: config)
    monkeypatch.setattr(item, "_runtime_quiescence_guard", guard)
    monkeypatch.setattr(item, "bootstrap_project", lambda _: False)
    monkeypatch.setattr(item, "synchronize_project_runtime", lambda *_: None)
    monkeypatch.setattr(item, "_allowed_projects", lambda: [])
    monkeypatch.setattr(item, "configure_openclaw", lambda _allowed, **_: None)
    monkeypatch.setattr(item, "install_launchd_plist", lambda _: None)
    monkeypatch.setattr(item, "activate_launchd", lambda: None)
    monkeypatch.setattr(item, "deactivate_launchd", lambda: None)
    monkeypatch.setattr(item, "mark_ready_or_schedule_build", lambda: ("READY", None))

    def write_health(**_: Any) -> None:
        item.health_receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        item.health_receipt_path.write_text("{}\n", encoding="utf-8")
        item.health_receipt_path.chmod(0o600)

    monkeypatch.setattr(item, "_write_health_receipt", write_health)
    if fail_activation:
        def fail(_transaction: dict[str, Any]) -> None:
            raise RuntimeError("forced v2 upgrade activation failure")

        monkeypatch.setattr(item, "_verify_activation_pending", fail)
    else:
        monkeypatch.setattr(item, "_verify_activation_pending", lambda _: None)
        monkeypatch.setattr(
            item,
            "verify",
            lambda: {
                "ok": True,
                "phase": "committed",
                "contractVersion": core.INTEGRATION_CONTRACT_VERSION,
            },
        )
    return config


def _cron_mutation_count(cli: StatefulCronCli) -> int:
    return sum(
        call[:2] in {
            ("cron", "add"),
            ("cron", "rm"),
            ("cron", "edit"),
            ("cron", "disable"),
        }
        for call in (tuple(entry) for entry in cli.calls)
    )


def test_exact_committed_v2_upgrade_succeeds_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    jobs_before = _seed_v2_install(item, cli)
    unknown_before = copy.deepcopy(jobs_before[-1])
    _prepare_upgrade_runtime(item, monkeypatch)

    result = item._integrate_locked({"runtimePort": 18888})

    assert result["transaction"] == "upgraded"
    transaction = item.store.read()
    assert transaction["phase"] == "committed"
    assert transaction["contractVersion"] == core.INTEGRATION_CONTRACT_VERSION
    assert transaction["previousContractVersion"] == 2
    assert transaction["ownership"] == item._ownership_payload()
    assert next(job for job in cli.jobs if job["id"] == "customer-unknown") == unknown_before
    assert len([
        job for job in cli.jobs
        if job.get("declarationKey") == core.CRON_DECLARATION_KEY
        and job.get("enabled") is True
    ]) == 1
    assert len([
        job for job in cli.jobs
        if job.get("declarationKey") == core.SNAPSHOT_CRON_DECLARATION_KEY
        and job.get("enabled") is True
    ]) == 1

    mutations_after_upgrade = _cron_mutation_count(cli)
    again = item._integrate_locked({"runtimePort": 18888})

    assert again["transaction"] == "already_current"
    assert _cron_mutation_count(cli) == mutations_after_upgrade
    assert next(job for job in cli.jobs if job["id"] == "customer-unknown") == unknown_before


def test_exact_committed_v2_forced_failure_rolls_back_complete_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = StatefulCronCli()
    item = manager(tmp_path, cli)
    jobs_before = _seed_v2_install(item, cli)
    config = _prepare_upgrade_runtime(item, monkeypatch, fail_activation=True)
    config_before = config.read_bytes()
    health_path = item.health_receipt_path

    with pytest.raises(RuntimeError, match="forced v2 upgrade activation failure"):
        item._integrate_locked({"runtimePort": 18888})

    transaction = item.store.read()
    assert transaction["phase"] == "rolled_back"
    assert transaction["previousContractVersion"] == 2
    assert transaction["rollbackOutcome"] == "restored_exactly"
    assert config.read_bytes() == config_before
    assert not health_path.exists()
    assert not item.paths.launchd_plist.exists()

    unknown_before = jobs_before[-1]
    assert next(job for job in cli.jobs if job["id"] == "customer-unknown") == unknown_before
    restored_managed = [
        job for job in cli.jobs
        if job.get("declarationKey") in {
            core.CRON_DECLARATION_KEY,
            core.SNAPSHOT_CRON_DECLARATION_KEY,
        }
    ]
    assert len(restored_managed) == 2
    for expected in jobs_before[:2]:
        restored = next(
            job for job in restored_managed
            if job.get("declarationKey") == expected.get("declarationKey")
        )
        assert core._job_contract_hash(restored) == core._job_contract_hash(expected)
    assert len(cli.jobs) == len(jobs_before)
    assert not any(
        path.name.startswith((
            ".qwen-asset-install-",
            ".qwen-asset-restore-",
            ".qwen-recovery-quarantine-",
        ))
        for root in (
            item.paths.project_root,
            item.plugin_target.parent,
            (item.paths.workspace / "skills"),
        )
        if root.exists()
        for path in root.iterdir()
    )
