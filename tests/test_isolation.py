import os
import pytest
from src.providers.qwen_local import QwenLocalProvider

def test_provider_isolation():
    # Should fail if using production directory
    with pytest.raises(PermissionError):
        QwenLocalProvider(root_dir="/Users/as_openclaw/.openclaw/workspace/lancedb/gemini")

    # Should succeed with shadow root
    os.environ["LANCEDB_SHADOW_ROOT"] = "/tmp/shadow_lancedb"
    provider = QwenLocalProvider()
    assert provider.provider_id == "qwen-local"
    assert provider.embed("test") == [0.0] * 768

