#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "openclaw-lancedb-knowledge-local" / "assets" / "knowledge-lancedb-template"
BOOTSTRAP = ROOT / "openclaw-lancedb-knowledge-local" / "scripts" / "bootstrap_openclaw_lancedb.py"

PRODUCTION_PATTERNS = {
    "node child process": re.compile(r"node:child_process|\b(?:exec|execFile|execSync|spawn|spawnSync)\s*\("),
    "dynamic evaluation": re.compile(r"\beval\s*\(|\bFunction\s*\("),
    "shell execution": re.compile(r"shell\s*:\s*true|\b(?:bash|sh)\s+-c\b"),
    "network bootstrap": re.compile(r"\b(?:curl|wget)\s+"),
}


def main() -> None:
    failures: list[str] = []
    production_files = [
        *sorted((TEMPLATE / "src").glob("*.js")),
        *sorted((TEMPLATE / "scripts").glob("*")),
    ]
    for path in production_files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in PRODUCTION_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{path.relative_to(ROOT)}: {label}")

    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    required_contract = [
        'command = [str(Path(npm_bin).resolve()), "ci"]',
        'command.append("--ignore-scripts")',
        'env["npm_config_ignore_scripts"] = "true"',
        "subprocess.run(command, cwd=target, check=True, shell=False, env=env)",
        'if args.allow_package_scripts and not args.npm_install:',
    ]
    for marker in required_contract:
        if marker not in bootstrap:
            failures.append(f"bootstrap contract missing: {marker}")
    if "shell=True" in bootstrap or "os.system" in bootstrap:
        failures.append("bootstrap contains shell execution")
    if bootstrap.count("subprocess.run(") != 1:
        failures.append("bootstrap must contain exactly one reviewed subprocess.run call")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        raise SystemExit(1)
    print(f"PASS dangerous-exec isolation ({len(production_files)} production files checked)")


if __name__ == "__main__":
    main()
