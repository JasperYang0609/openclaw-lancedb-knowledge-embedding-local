#!/usr/bin/env python3
"""Atomically point an active knowledge project symlink at a verified Qwen release."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


REQUIRED_PROVIDER = "qwen-local"
REQUIRED_RUNTIME = "b10625"
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def read_regular_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Required JSON must be a regular file: {path.name}")
    return json.loads(path.read_text())


def validate_candidate(candidate: Path) -> dict:
    if candidate.is_symlink() or not candidate.is_dir():
        raise RuntimeError("Candidate must be a non-symlink directory")
    config = read_regular_json(candidate / "config/source-map.json")
    manifest = read_regular_json(candidate / "reports/index-manifest.latest.json")
    state = read_regular_json(candidate / "data/index-state.json")
    embedding = config.get("embedding", {})
    endpoint = urlparse(embedding.get("endpoint", ""))
    if embedding.get("provider") != REQUIRED_PROVIDER:
        raise RuntimeError("Candidate provider is not qwen-local")
    if embedding.get("runtimeRevision") != REQUIRED_RUNTIME:
        raise RuntimeError("Candidate runtime identity is not b10625")
    if endpoint.scheme != "http" or endpoint.hostname not in LOOPBACK_HOSTS:
        raise RuntimeError("Candidate endpoint is not loopback HTTP")
    if config.get("privacy", {}).get("cloudFallback") not in {None, "DISABLED"}:
        raise RuntimeError("Candidate cloud fallback is not disabled")
    if manifest.get("embedding", {}).get("provider") != REQUIRED_PROVIDER:
        raise RuntimeError("Manifest provider mismatch")
    if manifest.get("embedding", {}).get("runtimeRevision") != REQUIRED_RUNTIME:
        raise RuntimeError("Manifest runtime identity mismatch")
    indexed = int(manifest.get("chunksIndexed") or 0)
    available = int(manifest.get("chunksAvailable") or 0)
    if indexed < 1 or indexed != available:
        raise RuntimeError("Candidate manifest is not a complete full index")
    if int(state.get("chunks") or 0) != indexed:
        raise RuntimeError("Candidate state row count does not match manifest")
    if state.get("embedding", {}).get("provider") != REQUIRED_PROVIDER:
        raise RuntimeError("Candidate state provider mismatch")
    db_path = (candidate / config.get("dbPath", "")).resolve()
    if candidate.resolve() not in db_path.parents or not db_path.is_dir():
        raise RuntimeError("Candidate database path is missing or escapes the release")
    return {"rows": indexed, "runtime": REQUIRED_RUNTIME, "provider": REQUIRED_PROVIDER}


def activate(active_link: Path, candidate: Path, receipt_dir: Path) -> dict:
    active_link = active_link.absolute()
    candidate = candidate.resolve()
    if active_link.parent != candidate.parent:
        raise RuntimeError("Active link and candidate must share a parent directory")
    if active_link.exists() and not active_link.is_symlink():
        raise RuntimeError("Active path must be a symlink or absent")
    evidence = validate_candidate(candidate)
    previous = str(active_link.resolve()) if active_link.is_symlink() else None
    receipt_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=active_link.parent) as temp_dir:
        temp_link = Path(temp_dir) / "active-link"
        temp_link.symlink_to(candidate.name, target_is_directory=True)
        os.replace(temp_link, active_link)
    receipt = {
        "schemaVersion": 1,
        "activatedAt": datetime.now(timezone.utc).isoformat(),
        "activeLink": str(active_link),
        "candidate": str(candidate),
        "previous": previous,
        **evidence,
    }
    receipt_path = receipt_dir / "local-release-activation.latest.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--active-link", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--receipt-dir", required=True)
    args = parser.parse_args()
    result = activate(Path(args.active_link), Path(args.candidate), Path(args.receipt_dir))
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
