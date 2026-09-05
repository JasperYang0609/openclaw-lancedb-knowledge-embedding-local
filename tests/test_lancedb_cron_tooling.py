import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "openclaw-lancedb-knowledge-local/assets/knowledge-lancedb-template/scripts/audit_cron_tooling.py"
spec = importlib.util.spec_from_file_location("lancedb_cron_tooling", SCRIPT)
cron = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cron)


def test_command_tools_allow_requires_recreation_without_unsupported_tools_edit():
    result = cron.audit_jobs({"jobs": [{
        "id": "snapshot-job",
        "enabled": True,
        "payload": {"kind": "command", "argv": ["/managed/wrapper"], "toolsAllow": []},
    }]})
    assert result["ok"] is False
    remediation = result["findings"][0]["remediation"]
    assert "recreate" in remediation
    assert "--clear-tools" not in remediation
    assert "--tools" not in remediation
    assert result["canary"]["successMarker"] == "TOOL_OK"


def test_agent_turn_tools_allow_can_use_clear_tools_remediation():
    result = cron.audit_jobs({"jobs": [{
        "id": "agent-job",
        "enabled": True,
        "payload": {"kind": "agentTurn", "model": "openai/gpt-5.5", "toolsAllow": []},
    }]})

    assert result["ok"] is False
    assert result["findings"][0]["remediation"].endswith("agent-job --clear-tools")
