from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts/rebuild_verify_activate.sh"


def test_runner_is_fail_closed_and_requires_full_verification_before_activation():
    source = SCRIPT.read_text()
    assert "set -euo pipefail" in source
    for variable in ("QWEN_CANDIDATE", "QWEN_ACTIVE_LINK", "QWEN_RECEIPT_DIR", "QWEN_ACTIVATOR"):
        assert f'${{{variable}:?' in source
    assert source.index("npm run index") < source.index("npm run audit")
    assert source.index("npm run audit") < source.index("--active-link")
    assert "QWEN_REBUILD_ACTIVATED" in source
