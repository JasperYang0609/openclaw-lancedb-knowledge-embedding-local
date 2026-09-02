#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "openclaw-lancedb-knowledge-local" / "scripts" / "bootstrap_openclaw_lancedb.py"


def make_fake_npm(bin_dir: Path, log_path: Path) -> Path:
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {str(log_path)!r}\n"
        f"printf 'cwd=%s\\n' \"$PWD\" >> {str(log_path)!r}\n"
        f"printf 'ignore_scripts=%s\\n' \"${{npm_config_ignore_scripts-}}\" >> {str(log_path)!r}\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)
    return npm


def run_bootstrap(target: Path, env: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--target",
            str(target),
            "--workspace",
            str(target.parent / "workspace"),
            *extra,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="lancedb-bootstrap-security-") as tmp_dir:
        tmp = Path(tmp_dir)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        env = {
            "PATH": str(bin_dir),
            "HOME": str(tmp / "home"),
            "TMPDIR": str(tmp),
        }

        default_log = tmp / "npm-default.log"
        fake_npm = make_fake_npm(bin_dir, default_log)
        default_target = tmp / "default-install"
        result = run_bootstrap(default_target, env, "--npm-install")
        assert result.returncode == 0, result.stderr
        default_args = default_log.read_text(encoding="utf-8").splitlines()
        assert default_args[:2] == ["ci", "--ignore-scripts"]
        assert default_args[2] == f"cwd={default_target}"
        assert default_args[3] == "ignore_scripts=true"
        payload = json.loads(result.stdout)
        assert payload["install_command"] == [str(fake_npm.resolve()), "ci", "--ignore-scripts"]
        default_config = json.loads((default_target / "config/source-map.json").read_text(encoding="utf-8"))
        assert all(source.get("sourceType") != "discord_raw" for source in default_config["sources"])

        port_target = tmp / "port-install"
        result = run_bootstrap(port_target, env, "--endpoint", "http://127.0.0.1:18889")
        assert result.returncode == 0, result.stderr
        port_config = json.loads((port_target / "config/source-map.json").read_text(encoding="utf-8"))
        assert port_config["embedding"]["endpoint"] == "http://127.0.0.1:18889"
        for unsafe in ("https://127.0.0.1:18889", "http://localhost:18889", "http://127.0.0.1:80",
                       "http://127.0.0.1:18889/path", "http://user@127.0.0.1:18889"):
            rejected = run_bootstrap(tmp / f"unsafe-{abs(hash(unsafe))}", env, "--endpoint", unsafe)
            assert rejected.returncode != 0
            assert "loopback HTTP" in (rejected.stderr + rejected.stdout)

        raw_target = tmp / "raw-install"
        result = run_bootstrap(
            raw_target,
            env,
            "--backup-root",
            str(tmp / "discord-backup"),
            "--include-discord-raw",
        )
        assert result.returncode == 0, result.stderr
        raw_config = json.loads((raw_target / "config/source-map.json").read_text(encoding="utf-8"))
        raw_sources = [source for source in raw_config["sources"] if source.get("sourceType") == "discord_raw"]
        assert len(raw_sources) == 1
        assert raw_sources[0]["include"] == ["**/raw/**/*.md"]
        assert raw_sources[0]["root"] == str((tmp / "discord-backup").resolve())

        allow_log = tmp / "npm-allow.log"
        make_fake_npm(bin_dir, allow_log)
        allow_target = tmp / "allow-install"
        result = run_bootstrap(
            allow_target,
            env,
            "--npm-install",
            "--allow-package-scripts",
        )
        assert result.returncode == 0, result.stderr
        allow_args = allow_log.read_text(encoding="utf-8").splitlines()
        assert allow_args[0] == "ci"
        assert "--ignore-scripts" not in allow_args
        assert allow_args[-1] == "ignore_scripts="

        result = run_bootstrap(tmp / "invalid-flags", env, "--allow-package-scripts")
        assert result.returncode != 0
        assert "requires --npm-install" in (result.stderr + result.stdout)

        no_npm_env = {"PATH": str(tmp / "empty-bin"), "HOME": str(tmp / "home")}
        (tmp / "empty-bin").mkdir()
        result = run_bootstrap(tmp / "no-npm", no_npm_env, "--npm-install")
        assert result.returncode != 0
        assert "npm executable not found" in (result.stderr + result.stdout)

    print("PASS test_bootstrap_security")


if __name__ == "__main__":
    main()
