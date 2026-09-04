from __future__ import annotations

import json
from types import SimpleNamespace
import subprocess
import sys
from pathlib import Path

import pytest

from src.openclaw_integration.core import IntegrationManager, IntegrationRollbackIncomplete

from scripts.qwen_local import (
    RuntimeHandoffRecoveryError,
    integrate_with_runtime_handoff,
    integration_manager,
    parser,
    recovery_error_fields,
    resolve_disabled_collision_approval,
    resolve_port,
    resolve_report_target,
)


ROOT = Path(__file__).resolve().parents[1]


def write_stored_ownership(
    state: Path,
    approval: object,
    *,
    schema_version: object = 1,
    phase: object = "committed",
) -> None:
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    manifest = state / "transaction.json"
    manifest.write_text(json.dumps({
        "schemaVersion": schema_version,
        "phase": phase,
        "ownership": {
            "schema": "qwen-local-openclaw.v2",
            "reportChannel": "discord",
            "reportTo": "channel:stored",
            "reportAccountId": "default",
            "approvedDisabledCollision": approval,
        },
    }), encoding="utf-8")
    manifest.chmod(0o600)


def test_status_is_redacted_for_uninstalled_target(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, str(ROOT / "scripts/qwen_local.py"), "status", "--target",
                             str(tmp_path / "managed/qwen")], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["installed"] is False
    assert payload["provider"] == "qwen-local"
    assert "api" not in result.stdout.lower()
    assert str(tmp_path) not in result.stdout


def test_unsupported_command_is_rejected_without_shell(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, str(ROOT / "scripts/qwen_local.py"), "install;echo-pwned",
                             "--target", str(tmp_path / "managed/qwen")], capture_output=True, text=True)
    assert result.returncode != 0
    assert "pwned\n" not in result.stdout


def test_resolve_port_uses_persisted_install_port(tmp_path: Path) -> None:
    class InstallerFixture:
        manifest_path = tmp_path / "install-manifest.json"

        def verify_installation(self):
            return {"runtimePort": 18890}

    InstallerFixture.manifest_path.write_text("{}")
    assert resolve_port(InstallerFixture(), None) == 18890
    assert resolve_port(InstallerFixture(), 18890) == 18890
    with pytest.raises(RuntimeError, match="differs from installed port"):
        resolve_port(InstallerFixture(), 18888)


def test_resolve_port_defaults_only_before_install(tmp_path: Path) -> None:
    class InstallerFixture:
        manifest_path = tmp_path / "missing-manifest.json"

    assert resolve_port(InstallerFixture(), None) == 18888
    assert resolve_port(InstallerFixture(), 18890) == 18890


def test_integration_manager_factory_returns_configured_manager(tmp_path: Path) -> None:
    args = SimpleNamespace(
        workspace=str(tmp_path / "workspace"),
        target=str(tmp_path / "runtime/qwen-local"),
        integration_state=str(tmp_path / "state/qwen-local-integration"),
        openclaw=sys.executable,
        node=sys.executable,
        profile="isolated-test",
        agent="main",
        command="integrate-openclaw",
        report_channel="discord",
        report_to="channel:1493072746702311474",
        report_account_id="default",
    )

    manager = integration_manager(args)

    assert isinstance(manager, IntegrationManager)
    assert manager.cli.profile == "isolated-test"
    assert manager.paths.project_root.name == "knowledge-lancedb-qwen-local"
    assert manager.report_channel == "discord"
    assert manager.report_to == "channel:1493072746702311474"
    assert manager.report_account_id == "default"


def test_parser_accepts_only_the_complete_incremental_disabled_collision_approval() -> None:
    help_text = parser().format_help()
    assert "ID-inclusive SHA-256" in help_text
    assert "--approve-disabled-collision-job-id" in help_text
    assert "--approve-disabled-collision-job-sha256" in help_text
    assert "--approve-disabled-collision-role" in help_text

    args = parser().parse_args([
        "integrate-openclaw",
        "--approve-disabled-collision-job-id", "legacy-disabled",
        "--approve-disabled-collision-job-sha256", "a" * 64,
        "--approve-disabled-collision-role", "incremental",
    ])
    approval = resolve_disabled_collision_approval(args, Path("/missing-private-state"))

    assert approval is not None
    assert approval.receipt() == {
        "jobId": "legacy-disabled",
        "contractSha256": "a" * 64,
        "role": "incremental",
    }

    with pytest.raises(SystemExit):
        parser().parse_args([
            "integrate-openclaw", "--approve-disabled-collision-role", "snapshot",
        ])


@pytest.mark.parametrize(
    "provided",
    [
        {"approve_disabled_collision_job_id": "legacy-disabled"},
        {"approve_disabled_collision_job_sha256": "a" * 64},
        {"approve_disabled_collision_role": "incremental"},
        {
            "approve_disabled_collision_job_id": "legacy-disabled",
            "approve_disabled_collision_job_sha256": "a" * 64,
        },
    ],
)
def test_disabled_collision_approval_cli_values_are_all_or_none(
    tmp_path: Path, provided: dict[str, str],
) -> None:
    args = SimpleNamespace(command="integrate-openclaw", **provided)
    with pytest.raises(RuntimeError, match="requires job id, SHA-256, and role together"):
        resolve_disabled_collision_approval(args, tmp_path / "state")


def test_no_flag_verify_reuses_exact_private_stored_collision_approval(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_stored_ownership(state, {
        "jobId": "legacy-disabled",
        "contractSha256": "b" * 64,
        "role": "incremental",
    })
    args = SimpleNamespace(command="verify-openclaw")

    approval = resolve_disabled_collision_approval(args, state)

    assert approval is not None
    assert approval.receipt()["contractSha256"] == "b" * 64


@pytest.mark.parametrize(
    "phase",
    ["prepared", "activation_pending", "failed", "rollback_failed", "rolled_back", None],
)
def test_non_committed_transaction_never_auto_authorizes_disabled_collision(
    tmp_path: Path, phase: object,
) -> None:
    state = tmp_path / "state"
    write_stored_ownership(state, {
        "jobId": "legacy-disabled",
        "contractSha256": "b" * 64,
        "role": "incremental",
    }, phase=phase)

    assert resolve_disabled_collision_approval(
        SimpleNamespace(command="verify-openclaw"), state,
    ) is None


@pytest.mark.parametrize("schema_version", [0, 2, True, "1", None])
def test_wrong_transaction_schema_version_never_auto_authorizes_disabled_collision(
    tmp_path: Path, schema_version: object,
) -> None:
    state = tmp_path / "state"
    write_stored_ownership(state, {
        "jobId": "legacy-disabled",
        "contractSha256": "b" * 64,
        "role": "incremental",
    }, schema_version=schema_version)

    assert resolve_disabled_collision_approval(
        SimpleNamespace(command="integrate-openclaw"), state,
    ) is None


def test_explicit_complete_collision_approval_overrides_stored_receipt(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_stored_ownership(state, {
        "jobId": "stored",
        "contractSha256": "b" * 64,
        "role": "incremental",
    })
    args = SimpleNamespace(
        command="integrate-openclaw",
        approve_disabled_collision_job_id="explicit",
        approve_disabled_collision_job_sha256="c" * 64,
        approve_disabled_collision_role="incremental",
    )

    approval = resolve_disabled_collision_approval(args, state)

    assert approval is not None and approval.job_id == "explicit"


@pytest.mark.parametrize(
    "stored",
    [
        {"jobId": "legacy", "contractSha256": "a" * 64},
        {
            "jobId": "legacy", "contractSha256": "a" * 64,
            "role": "incremental", "extra": "rejected",
        },
        {"jobId": "legacy", "contractSha256": "not-a-hash", "role": "incremental"},
        {"jobId": "legacy", "contractSha256": "a" * 64, "role": "snapshot"},
    ],
)
def test_malformed_stored_collision_approval_fails_closed_for_verify(
    tmp_path: Path, stored: dict[str, str],
) -> None:
    state = tmp_path / "state"
    write_stored_ownership(state, stored)
    with pytest.raises(RuntimeError, match="receipt is malformed"):
        resolve_disabled_collision_approval(SimpleNamespace(command="verify-openclaw"), state)


def test_malformed_optional_collision_receipt_never_blocks_rollback_recovery(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_stored_ownership(state, {"malformed": True})

    assert resolve_disabled_collision_approval(
        SimpleNamespace(command="rollback-openclaw"), state,
    ) is None


def test_fresh_integration_blocks_missing_or_ambiguous_failure_alert_target(tmp_path: Path) -> None:
    state = tmp_path / "state"
    missing = SimpleNamespace(command="integrate-openclaw", report_channel="", report_to="")
    partial = SimpleNamespace(command="integrate-openclaw", report_channel="discord", report_to="")

    with pytest.raises(RuntimeError, match="Explicit"):
        resolve_report_target(missing, state)
    with pytest.raises(RuntimeError, match="both"):
        resolve_report_target(partial, state)


def test_existing_v2_install_derives_exact_stored_failure_alert_target(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    manifest = state / "transaction.json"
    manifest.write_text(json.dumps({
        "schemaVersion": 1,
        "phase": "committed",
        "ownership": {
            "schema": "qwen-local-openclaw.v2",
            "reportChannel": "discord",
            "reportTo": "channel:stored",
            "reportAccountId": "default",
        },
    }), encoding="utf-8")
    manifest.chmod(0o600)
    args = SimpleNamespace(command="verify-openclaw", report_channel="", report_to="")

    assert resolve_report_target(args, state) == ("discord", "channel:stored", "default")


def test_runtime_handoff_restores_manual_service_when_integration_fails() -> None:
    events = []

    class RuntimeFixture:
        def status(self):
            return {"running": True}

        def stop(self):
            events.append("stop")

        def start(self):
            events.append("start")

    class IntegrationFixture:
        def integrate(self, _manifest):
            events.append("integrate")
            raise RuntimeError("fixture failure")

    with pytest.raises(RuntimeError, match="fixture failure"):
        integrate_with_runtime_handoff(
            runtime_manager=RuntimeFixture(), integration=IntegrationFixture(), runtime_manifest={"fixture": True},
        )
    assert events == ["stop", "integrate", "start"]


def test_runtime_handoff_does_not_restart_when_automatic_rollback_is_incomplete() -> None:
    events: list[str] = []
    primary = ValueError("primary integration fault")
    rollback = OSError("rollback verification fault")

    class RuntimeFixture:
        def status(self):
            return {"running": True}

        def stop(self):
            events.append("stop")

        def start(self):
            events.append("start")

    class IntegrationFixture:
        def integrate(self, _manifest):
            events.append("integrate")
            raise IntegrationRollbackIncomplete(primary, rollback)

    with pytest.raises(IntegrationRollbackIncomplete) as caught:
        integrate_with_runtime_handoff(
            runtime_manager=RuntimeFixture(),
            integration=IntegrationFixture(),
            runtime_manifest={"fixture": True},
        )

    assert events == ["stop", "integrate"]
    assert caught.value.original_error is primary
    assert caught.value.rollback_error is rollback
    assert recovery_error_fields(caught.value) == {
        "recoveryState": "automatic_rollback_incomplete",
        "primaryErrorType": "ValueError",
        "rollbackErrorType": "OSError",
        "manualRuntimeRestart": "not_attempted",
    }


def test_runtime_handoff_aggregates_restart_failure_without_masking_primary() -> None:
    events: list[str] = []
    primary = ValueError("primary integration fault")
    restart = OSError("manual runtime restart fault")

    class RuntimeFixture:
        def status(self):
            return {"running": True}

        def stop(self):
            events.append("stop")

        def start(self):
            events.append("start")
            raise restart

    class IntegrationFixture:
        def integrate(self, _manifest):
            events.append("integrate")
            raise primary

    with pytest.raises(RuntimeHandoffRecoveryError) as caught:
        integrate_with_runtime_handoff(
            runtime_manager=RuntimeFixture(),
            integration=IntegrationFixture(),
            runtime_manifest={"fixture": True},
        )

    assert events == ["stop", "integrate", "start"]
    assert caught.value.original_error is primary
    assert caught.value.restart_error is restart
    assert caught.value.__cause__ is primary
    assert recovery_error_fields(caught.value) == {
        "recoveryState": "manual_runtime_restart_failed",
        "primaryErrorType": "ValueError",
        "restartErrorType": "OSError",
        "manualRuntimeRestart": "failed",
    }
