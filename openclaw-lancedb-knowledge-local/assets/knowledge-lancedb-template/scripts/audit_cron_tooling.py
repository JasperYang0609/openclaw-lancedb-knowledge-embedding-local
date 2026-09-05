#!/usr/bin/env python3
"""Fail closed when legacy cron payload.toolsAllow can suppress GPT/Codex bash."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def audit_jobs(data: Any) -> dict[str, Any]:
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError("cron JSON must be an array or contain jobs[]")
    findings = []
    for job in jobs:
        if not job.get("enabled", True):
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if "toolsAllow" not in payload:
            continue
        job_id = str(job.get("id") or "")
        if payload.get("kind") == "agentTurn":
            remediation = f"openclaw cron edit {job_id} --clear-tools"
        else:
            remediation = (
                "review, disable, and recreate this command cron without payload.toolsAllow; "
                "command cron edit must not use tools flags"
            )
        findings.append({
            "jobId": job_id,
            "name": str(job.get("name") or job.get("id") or "unnamed"),
            "code": "LEGACY_PAYLOAD_TOOLS_ALLOW",
            "remediation": remediation,
        })
    return {
        "schema": "openclaw-lancedb-cron-tooling-audit-v1",
        "ok": not findings,
        "legacyToolsAllow": len(findings),
        "findings": findings,
        "canary": {
            "sessionTarget": "isolated",
            "payloadToolsAllowMustBeAbsent": True,
            "command": "pwd && echo TOOL_OK",
            "successMarker": "TOOL_OK",
            "removeAfterSuccess": True,
        },
        "rule": (
            "toolsAllow must be absent. agentTurn jobs may use --clear-tools; command jobs "
            "must be reviewed and recreated without any tools edit flag."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit cron JSON before enabling LanceDB shell jobs.")
    parser.add_argument("--input")
    parser.add_argument("--out")
    args = parser.parse_args()
    raw = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
    result = audit_jobs(json.loads(raw))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
