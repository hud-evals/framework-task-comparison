"""Minimal HUD environment for the orders incident demo."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hud import Environment
from hud.tools import BashTool, EditTool
from hud.tools.filesystem import GlobTool, GrepTool, ListTool, ReadTool
from hud.tools.types import EvaluationResult, SubScore

from linear_mcp import LinearService

ROOT_DIR = Path(__file__).resolve().parent
PROMPT = (
    "You have been assigned Linear issue ENG-450. Start in Linear and read the ticket details.\n\n"
    "The orders API source code is available locally at {workspace}. "
    "It is a Python FastAPI application.\n\n"
    "This looks like a production regression from last night's deploy. Customers are reporting "
    "bad checkout totals, but the ticket has the best context on what is and is not broken. "
    "Use that context to diagnose the issue and fix the root cause.\n\n"
    "Once fixed:\n"
    "1. Commit your changes to a new branch\n"
    "2. Push the branch to origin\n"
    "3. Leave a comment on the Linear issue summarizing the diagnosis and fix\n"
    "4. Mark the Linear issue as Done\n"
)


def _snapshot_refs(bare_repo: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=bare_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}

    refs: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            refs[parts[0]] = parts[1]
    return refs


def _detect_pushes(bare_repo: Path, initial_refs: dict[str, str]) -> list[dict[str, str]]:
    pushes: list[dict[str, str]] = []
    for ref, sha in _snapshot_refs(bare_repo).items():
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


IN_DOCKER = Path("/.dockerenv").exists()
RUNTIME_ROOT = Path("/home/ubuntu" if IN_DOCKER else ROOT_DIR / ".runtime" / "dev")
WORKSPACE = RUNTIME_ROOT / "workspace" / "orders_api"
BARE_REPO = RUNTIME_ROOT / "git" / "orders-api.git"
GRADING_DIR = RUNTIME_ROOT / "grading" / "orders_api"
LINEAR = LinearService()
INITIAL_REFS: dict[str, str] = {}


def attach(env: Environment) -> None:
    env.add_tool(BashTool())
    env.add_tool(EditTool())
    env.add_tool(ReadTool(base_path=str(WORKSPACE)))
    env.add_tool(GrepTool(base_path=str(WORKSPACE)))
    env.add_tool(GlobTool(base_path=str(WORKSPACE)))
    env.add_tool(ListTool(base_path=str(WORKSPACE)))
    env.connect_server(LINEAR.server, prefix="linear")


async def setup() -> str:
    for path in [WORKSPACE, BARE_REPO, GRADING_DIR]:
        shutil.rmtree(path, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(ROOT_DIR / "source" / "orders_api", WORKSPACE)
    subprocess.run(["git", "init"], cwd=WORKSPACE, check=True)
    subprocess.run(["git", "checkout", "-b", "order_bug_baseline"], cwd=WORKSPACE, check=True)
    subprocess.run(["git", "config", "user.name", "Agent Bot"], cwd=WORKSPACE, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=WORKSPACE, check=True)
    subprocess.run(["git", "add", "."], cwd=WORKSPACE, check=True)
    subprocess.run(["git", "commit", "-m", "Seed orders-api baseline"], cwd=WORKSPACE, check=True)
    subprocess.run(["git", "clone", "--bare", str(WORKSPACE), str(BARE_REPO)], check=True)
    INITIAL_REFS.clear()
    INITIAL_REFS.update(_snapshot_refs(BARE_REPO))
    subprocess.run(["git", "remote", "add", "origin", str(BARE_REPO)], cwd=WORKSPACE, check=True)
    LINEAR.configure(data_dir=str(ROOT_DIR / "linear_data"))

    if IN_DOCKER:
        subprocess.run(["chown", "-R", "1000:1000", str(WORKSPACE.parent)], check=False)

    return PROMPT.format(workspace=WORKSPACE)


async def grade(answer: Any) -> EvaluationResult:
    pushes = _detect_pushes(BARE_REPO, INITIAL_REFS)

    shutil.rmtree(GRADING_DIR, ignore_errors=True)
    subprocess.run(["git", "clone", str(BARE_REPO), str(GRADING_DIR)], check=True)

    grading_branch = pushes[-1]["branch"] if pushes else "order_bug_baseline"
    grading_commit = pushes[-1]["new_sha"] if pushes else subprocess.run(
        ["git", "rev-parse", "refs/heads/order_bug_baseline"],
        cwd=BARE_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    subprocess.run(["git", "checkout", grading_commit], cwd=GRADING_DIR, check=True)
    (GRADING_DIR / "test_order_pricing.py").write_text(
        (ROOT_DIR / "source" / "tests" / "test_order_pricing.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    test_result = subprocess.run(
        ["python", "-m", "pytest", "test_order_pricing.py", "-v"],
        cwd=GRADING_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    issue = LINEAR.data.get_issue("ENG-450")
    issue_state = (issue or {}).get("state") or {}
    comments = (getattr(LINEAR.data, "_created_comments", {}) or {}).get("issue-450", [])

    subscores = [
        SubScore(
            name="tests_pass",
            weight=0.8,
            value=1.0 if test_result.returncode == 0 else 0.0,
            metadata={
                "exit_code": test_result.returncode,
                "stdout": test_result.stdout[-4000:],
                "stderr": test_result.stderr[-4000:],
            },
        ),
        SubScore(
            name="branch_pushed",
            weight=0.1,
            value=1.0 if pushes else 0.0,
            metadata={"pushes": pushes},
        ),
        SubScore(
            name="linear_workflow_complete",
            weight=0.1,
            value=1.0 if issue_state.get("type") == "completed" and comments else 0.0,
            metadata={
                "linear_state": issue_state,
                "created_comment_count": len(comments),
            },
        ),
    ]

    return EvaluationResult(
        reward=sum(score.weight * score.value for score in subscores),
        done=True,
        content="Checked the hidden test, git push, and Linear workflow against the local sandbox.",
        subscores=subscores,
        info={
            "grading_branch": grading_branch,
            "grading_commit": grading_commit,
            "workspace": str(WORKSPACE),
            "agent_answer_preview": str(answer)[:500] if answer is not None else "",
        },
    )


env = Environment("orders-incident-hud")
attach(env)


@env.scenario("orders_incident")
async def orders_incident():
    answer = yield await setup()
    yield await grade(answer)


if __name__ == "__main__":
    env.run(transport="stdio")
