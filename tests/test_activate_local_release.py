import json
from pathlib import Path

import pytest

from scripts.activate_local_release import activate, validate_candidate


def make_candidate(root: Path, name: str = "release-next") -> Path:
    candidate = root / name
    (candidate / "config").mkdir(parents=True)
    (candidate / "reports").mkdir()
    (candidate / "data/index").mkdir(parents=True)
    embedding = {
        "provider": "qwen-local",
        "runtimeRevision": "b10625",
        "endpoint": "http://127.0.0.1:18889",
    }
    config = {
        "dbPath": "./data/index",
        "privacy": {"cloudFallback": "DISABLED"},
        "embedding": embedding,
    }
    manifest = {"chunksIndexed": 12, "chunksAvailable": 12, "embedding": embedding}
    state = {"chunks": 12, "embedding": embedding}
    (candidate / "config/source-map.json").write_text(json.dumps(config))
    (candidate / "reports/index-manifest.latest.json").write_text(json.dumps(manifest))
    (candidate / "data/index-state.json").write_text(json.dumps(state))
    return candidate


def test_activate_atomically_switches_relative_symlink_and_writes_receipt(tmp_path):
    old = make_candidate(tmp_path, "release-old")
    candidate = make_candidate(tmp_path)
    active = tmp_path / "active"
    active.symlink_to(old.name, target_is_directory=True)
    result = activate(active, candidate, tmp_path / "receipts")
    assert active.resolve() == candidate.resolve()
    assert result["previous"] == str(old.resolve())
    assert result["rows"] == 12
    assert (tmp_path / "receipts/local-release-activation.latest.json").is_file()


def test_rejects_cloud_endpoint(tmp_path):
    candidate = make_candidate(tmp_path)
    config_path = candidate / "config/source-map.json"
    config = json.loads(config_path.read_text())
    config["embedding"]["endpoint"] = "https://example.com"
    config_path.write_text(json.dumps(config))
    with pytest.raises(RuntimeError, match="loopback"):
        validate_candidate(candidate)


def test_rejects_partial_index(tmp_path):
    candidate = make_candidate(tmp_path)
    manifest_path = candidate / "reports/index-manifest.latest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["chunksIndexed"] = 11
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="complete full index"):
        validate_candidate(candidate)
