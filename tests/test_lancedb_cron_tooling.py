import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "openclaw-lancedb-knowledge-local/assets/knowledge-lancedb-template/scripts/audit_cron_tooling.py"
spec = importlib.util.spec_from_file_location("lancedb_cron_tooling", SCRIPT)
cron = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cron)


def test_legacy_tools_allow_requires_clear_tools_and_canary():
    result = cron.audit_jobs({"jobs": [{
        "id": "snapshot-job",
        "enabled": True,
        "payload": {"model": "openai/gpt-5.5", "toolsAllow": []},
    }]})
    assert result["ok"] is False
    assert result["findings"][0]["remediation"].endswith("snapshot-job --clear-tools")
    assert result["canary"]["successMarker"] == "TOOL_OK"
