from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FULL = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
BRANCH = (ROOT / ".github/workflows/branch-check.yml").read_text(encoding="utf-8")


def require(source: str, text: str, label: str) -> None:
    if text not in source:
        raise AssertionError(f"{label} is missing required contract: {text}")


def reject(source: str, text: str, label: str) -> None:
    if text in source:
        raise AssertionError(f"{label} contains forbidden contract: {text}")


for required in (
    "branches: [main]",
    "pull_request:",
    "workflow_dispatch:",
    "cancel-in-progress: true",
    "python -m pytest -q",
    "npm exec -- openclaw plugins validate",
    "python3 tests/test_snapshot_knowledge_assets.py",
    "python3 scripts/check_dangerous_exec.py",
    "python3 scripts/build_skill_archive.py --check",
):
    require(FULL, required, "full CI")
reject(FULL, 'branches: [main, "feat/**", "fix/**"]', "full CI")

for required in (
    "branches-ignore: [main]",
    "group: branch-check-${{ github.ref }}",
    "cancel-in-progress: true",
    "pull-requests: read",
    "gh api --method GET",
    "if: needs.detect-open-pr.outputs.exists != 'true'",
    "bash scripts/check_push.sh",
):
    require(BRANCH, required, "branch CI")

for forbidden in ("test_snapshot_knowledge_assets.py", "pytest -q\n"):
    reject(BRANCH, forbidden, "branch CI")

print("CI workflow contract checks passed")
