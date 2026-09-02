from __future__ import annotations

import json
from types import SimpleNamespace
import subprocess
import sys
from pathlib import Path

import pytest

from src.openclaw_integration.core import IntegrationManager

from scripts.qwen_local import integrate_with_runtime_handoff, integration_manager, resolve_port


ROOT = Path(__file__).resolve().parents[1]


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
    )

    manager = integration_manager(args)

    assert isinstance(manager, IntegrationManager)
    assert manager.cli.profile == "isolated-test"
    assert manager.paths.project_root.name == "knowledge-lancedb-qwen-local"


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
