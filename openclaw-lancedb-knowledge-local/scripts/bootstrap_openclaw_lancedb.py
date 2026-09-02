#!/usr/bin/env python3
"""Bootstrap an isolated Qwen-local LanceDB knowledge project."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

SAFE_INSTALL_ENV_KEYS = ("HOME", "PATH", "TMPDIR", "TMP", "TEMP", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                         "SSL_CERT_FILE", "SSL_CERT_DIR")


def copytree(src: Path, dst: Path, overwrite: bool) -> None:
    if dst.exists() and any(dst.iterdir()) and not overwrite:
        raise SystemExit(f"Target exists and is not empty: {dst}")
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


def install_dependencies(target: Path, allow_package_scripts: bool = False) -> list[str]:
    npm_bin = shutil.which("npm")
    if not npm_bin:
        raise SystemExit("npm executable not found")
    command = [str(Path(npm_bin).resolve()), "ci"]
    env = {key: os.environ[key] for key in SAFE_INSTALL_ENV_KEYS if os.environ.get(key)}
    if not allow_package_scripts:
        command.append("--ignore-scripts")
        env["npm_config_ignore_scripts"] = "true"
    subprocess.run(command, cwd=target, check=True, shell=False, env=env)
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Qwen local-only OpenClaw LanceDB project")
    parser.add_argument("--target", default="~/.openclaw/workspace/knowledge-lancedb-qwen-local")
    parser.add_argument("--workspace", default="~/.openclaw/workspace")
    parser.add_argument("--backup-root", default="")
    parser.add_argument("--include-discord-raw", action="store_true")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--project-name", default="ClientProject")
    parser.add_argument("--api-key-file", default="~/Library/Application Support/OpenClaw/qwen-local/run/api-key")
    parser.add_argument("--endpoint", default="http://127.0.0.1:18888")
    parser.add_argument("--npm-install", action="store_true")
    parser.add_argument("--allow-package-scripts", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.allow_package_scripts and not args.npm_install:
        raise SystemExit("--allow-package-scripts requires --npm-install")
    endpoint = urllib.parse.urlparse(args.endpoint)
    if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.username or
            endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"} or
            endpoint.port is None or not 1024 <= endpoint.port <= 65535):
        raise SystemExit("The Qwen endpoint must be loopback HTTP on an explicit unprivileged port")

    skill_dir = Path(__file__).resolve().parents[1]
    template = skill_dir / "assets/knowledge-lancedb-template"
    target = Path(args.target).expanduser().resolve()
    if target in {Path.home().resolve(), (Path.home() / ".openclaw/workspace").resolve()}:
        raise SystemExit("Refusing to bootstrap into a protected root")
    copytree(template, target, args.overwrite)
    (target / "data/qwen-local-lancedb").mkdir(parents=True, exist_ok=True)
    (target / "reports/cron-logs").mkdir(parents=True, exist_ok=True)
    cfg = json.loads((target / "config/source-map.example.json").read_text())
    replacements = {
        "__OPENCLAW_WORKSPACE__/memory": str(Path(args.workspace).expanduser().resolve() / "memory"),
        "__DISCORD_BACKUP_ROOT__": str(Path(args.backup_root).expanduser().resolve()) if args.backup_root else "__DISCORD_BACKUP_ROOT__",
        "__PROJECT_DOC_ROOT__": str(Path(args.project_root).expanduser().resolve()) if args.project_root else "__PROJECT_DOC_ROOT__",
    }
    for source in cfg["sources"]:
        source["root"] = replacements.get(source["root"], source["root"])
        if source["id"] == "project-docs":
            source["project"] = args.project_name
    if args.include_discord_raw:
        cfg["sources"].append({"id": "discord-backup-raw", "project": "DiscordBackups", "sourceType": "discord_raw",
                               "root": replacements["__DISCORD_BACKUP_ROOT__"], "include": ["**/raw/**/*.md"],
                               "exclude": ["**/.env*", "**/*secret*", "**/*token*"], "priority": 1})
        cfg["privacy"] = {"discordRawApproval": "LOCAL_ONLY", "exactMessageIdValidation": "REQUIRED"}
    cfg["embedding"]["apiKeyFile"] = str(Path(args.api_key_file).expanduser().resolve())
    cfg["embedding"]["endpoint"] = f"http://127.0.0.1:{endpoint.port}"
    (target / "config/source-map.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    command = install_dependencies(target, args.allow_package_scripts) if args.npm_install else None
    print(json.dumps({"ok": True, "provider": "qwen-local", "target": str(target), "install_command": command,
                      "next": ["npm ci --ignore-scripts", "npm test", "npm run scan", "npm run index"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
