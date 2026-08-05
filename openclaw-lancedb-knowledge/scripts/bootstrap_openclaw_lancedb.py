#!/usr/bin/env python3
"""Bootstrap a portable OpenClaw LanceDB knowledge index project from this skill."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def copytree(src: Path, dst: Path, overwrite: bool) -> None:
    if dst.exists() and any(dst.iterdir()) and not overwrite:
        raise SystemExit(f"Target exists and is not empty: {dst}\nUse --overwrite to replace template files.")
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in {"node_modules", "data", "reports"}:
            continue
        target = dst / item.name
        if item.is_dir():
            if target.exists() and overwrite:
                shutil.rmtree(target)
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


SAFE_INSTALL_ENV_KEYS = (
    "HOME",
    "PATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def install_dependencies(target: Path, allow_package_scripts: bool = False) -> list[str]:
    """Run a fixed npm install contract without a shell or dynamic arguments."""
    npm_bin = shutil.which("npm")
    if not npm_bin:
        raise SystemExit("npm executable not found on PATH; dependencies were not installed.")
    command = [str(Path(npm_bin).resolve()), "ci"]
    env = {key: os.environ[key] for key in SAFE_INSTALL_ENV_KEYS if os.environ.get(key)}
    if not allow_package_scripts:
        command.append("--ignore-scripts")
        env["npm_config_ignore_scripts"] = "true"
    subprocess.run(command, cwd=target, check=True, shell=False, env=env)
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an OpenClaw-friendly LanceDB knowledge index project.")
    parser.add_argument("--target", default="~/.openclaw/workspace/knowledge-lancedb", help="Install target directory")
    parser.add_argument("--workspace", default="~/.openclaw/workspace", help="OpenClaw workspace path")
    parser.add_argument("--backup-root", default="", help="Discord/channel backup root containing summary/ markdown")
    parser.add_argument("--project-root", default="", help="Client/project docs root to index")
    parser.add_argument("--project-name", default="ClientProject", help="Project label stored in LanceDB rows")
    parser.add_argument("--google-gemini", action="store_true", help="Use Google Gemini embeddings instead of local hash embeddings")
    parser.add_argument("--embedding-profile", choices=["balanced", "high-quality"], default="balanced", help="Balanced uses 768 Gemini dimensions; high-quality uses 3072 and requires a full rebuild")
    parser.add_argument("--approved-by", default="", help="Required note when enabling external embeddings")
    parser.add_argument("--npm-install", action="store_true", help="Run npm ci --ignore-scripts after copying files")
    parser.add_argument("--allow-package-scripts", action="store_true", help="Explicitly allow npm lifecycle scripts; requires --npm-install and dependency review")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing template files in target")
    args = parser.parse_args()
    if args.allow_package_scripts and not args.npm_install:
        raise SystemExit("--allow-package-scripts requires --npm-install.")

    skill_dir = Path(__file__).resolve().parents[1]
    template = skill_dir / "assets" / "knowledge-lancedb-template"
    if not template.exists():
        raise SystemExit(f"Template not found: {template}")

    target = Path(args.target).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()
    backup_root = Path(args.backup_root).expanduser().resolve() if args.backup_root else Path("__DISCORD_BACKUP_ROOT__")
    project_root = Path(args.project_root).expanduser().resolve() if args.project_root else Path("__PROJECT_DOC_ROOT__")

    if args.google_gemini and not args.approved_by:
        raise SystemExit("--approved-by is required with --google-gemini because private chunks leave the machine for embedding.")
    if args.embedding_profile == "high-quality" and not args.google_gemini:
        raise SystemExit("--embedding-profile high-quality currently requires --google-gemini.")

    copytree(template, target, args.overwrite)
    (target / "data").mkdir(exist_ok=True)
    (target / "reports" / "cron-logs").mkdir(parents=True, exist_ok=True)

    example_cfg = target / "config" / "source-map.example.json"
    cfg = json.loads(example_cfg.read_text())
    for src in cfg["sources"]:
        if src["root"] == "__OPENCLAW_WORKSPACE__/memory":
            src["root"] = str(workspace / "memory")
        elif src["root"] == "__DISCORD_BACKUP_ROOT__":
            src["root"] = str(backup_root)
        elif src["root"] == "__PROJECT_DOC_ROOT__":
            src["root"] = str(project_root)
            src["project"] = args.project_name

    if args.google_gemini:
        dimensions = 3072 if args.embedding_profile == "high-quality" else 768
        cfg["embedding"] = {
            "provider": "google-gemini",
            "model": "gemini-embedding-001",
            "profile": args.embedding_profile,
            "dimensions": dimensions,
            "documentTaskType": "RETRIEVAL_DOCUMENT",
            "queryTaskType": "RETRIEVAL_QUERY",
            "batchSize": 40,
            "throttleMs": 250,
            "cachePath": f"./data/embedding-cache/google-gemini-embedding-001-{dimensions}.jsonl",
            "privacyApprovedAt": datetime.now(timezone.utc).isoformat(),
            "privacyApprovedBy": args.approved_by,
        }
        if args.embedding_profile == "high-quality":
            cfg["chunking"] = {"maxChars": 2800, "overlapChars": 350}

    (target / "config" / "source-map.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")

    install_command = None
    if args.npm_install:
        install_command = install_dependencies(target, args.allow_package_scripts)

    print(json.dumps({
        "ok": True,
        "target": str(target),
        "config": str(target / "config" / "source-map.json"),
        "install_command": install_command,
        "next": [
            "cd " + str(target),
            "npm ci --ignore-scripts" if not args.npm_install else "npm test",
            "npm run scan",
            "npm run index",
            "npm run search -- \"project status\" -- --limit 5",
        ]
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
