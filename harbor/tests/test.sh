#!/bin/bash

cd /app

python3 - <<'PY'
import json
import shutil
import subprocess
from pathlib import Path

APP_DIR = Path("/app")
BARE_REPO = APP_DIR / "orders-api.git"
INITIAL_REFS_PATH = APP_DIR / "initial_refs.json"
LINEAR_ISSUES_PATH = APP_DIR / "linear_data" / "issues.json"
COMMENTS_PATH = APP_DIR / "linear_data" / "comments.json"
GRADING_DIR = Path("/tmp/grading/orders_api")
HIDDEN_TEST = Path("/tests/test_order_pricing.py")
REWARD_PATH = Path("/logs/verifier/reward.txt")
DETAILS_PATH = Path("/logs/verifier/details.json")


def snapshot_refs() -> dict[str, str]:
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=BARE_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    refs: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            refs[parts[0]] = parts[1]
    return refs


def detect_pushes(initial_refs: dict[str, str]) -> list[dict[str, str]]:
    pushes: list[dict[str, str]] = []
    for ref, sha in snapshot_refs().items():
        if not ref.startswith("refs/heads/"):
            continue
        old_sha = initial_refs.get(ref)
        if old_sha != sha:
            pushes.append(
                {
                    "branch": ref.removeprefix("refs/heads/"),
                    "old_sha": old_sha or "(new)",
                    "new_sha": sha,
                }
            )
    return pushes


initial_refs = json.loads(INITIAL_REFS_PATH.read_text(encoding="utf-8"))
pushes = detect_pushes(initial_refs)

tests_pass = 0.0
branch_pushed = 1.0 if pushes else 0.0
linear_workflow_complete = 0.0

shutil.rmtree(GRADING_DIR, ignore_errors=True)
GRADING_DIR.parent.mkdir(parents=True, exist_ok=True)

clone_result = subprocess.run(
    ["git", "clone", "--no-hardlinks", str(BARE_REPO), str(GRADING_DIR)],
    capture_output=True,
    text=True,
    check=False,
)
if clone_result.returncode != 0:
    print(clone_result.stdout)
    print(clone_result.stderr)
else:
    if pushes:
        grading_commit = pushes[-1]["new_sha"]
        grading_branch = pushes[-1]["branch"]
    else:
        grading_commit = subprocess.run(
            ["git", "rev-parse", "refs/heads/order_bug_baseline"],
            cwd=BARE_REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        grading_branch = "order_bug_baseline"

    subprocess.run(
        ["git", "checkout", grading_commit],
        cwd=GRADING_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    shutil.copy(HIDDEN_TEST, GRADING_DIR / "test_order_pricing.py")
    test_result = subprocess.run(
        ["python3", "-m", "pytest", "test_order_pricing.py", "-v"],
        cwd=GRADING_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    tests_pass = 1.0 if test_result.returncode == 0 else 0.0
    print(f"grading_branch={grading_branch}")
    print(f"grading_commit={grading_commit}")
    print(test_result.stdout[-4000:])
    if test_result.stderr:
        print(test_result.stderr[-4000:])

issue = json.loads(LINEAR_ISSUES_PATH.read_text(encoding="utf-8"))[0]
comments = json.loads(COMMENTS_PATH.read_text(encoding="utf-8"))
linear_workflow_complete = 1.0 if issue["state"]["type"] == "completed" and comments else 0.0

reward = (0.8 * tests_pass) + (0.1 * branch_pushed) + (0.1 * linear_workflow_complete)
payload = {
    "reward": reward,
    "tests_pass": tests_pass,
    "branch_pushed": branch_pushed,
    "linear_workflow_complete": linear_workflow_complete,
}
REWARD_PATH.write_text(f"{reward}\n", encoding="utf-8")
DETAILS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2))
PY
