from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inspect_ai import Task, task
from inspect_ai.agent import react
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import ToolError, bash_session, text_editor, tool
from inspect_ai.util import sandbox, store

ROOT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = ROOT_DIR / "assets"
ORDERS_API_DIR = ASSETS_DIR / "orders_api"
HIDDEN_TEST_PATH = ASSETS_DIR / "tests" / "test_order_pricing.py"
LINEAR_ISSUES_PATH = ASSETS_DIR / "linear_data" / "issues.json"
WORKFLOW_STATES_PATH = ASSETS_DIR / "linear_data" / "workflow_states.json"

WORKSPACE = "workspace/orders_api"
BARE_REPO = "workspace/orders-api.git"
GRADING_DIR = "workspace/grading/orders_api"
DEFAULT_BRANCH = "order_bug_baseline"

PROMPT_TEMPLATE = (
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
PROMPT = PROMPT_TEMPLATE.format(workspace=WORKSPACE)

VIEWER = {
    "id": "user-001",
    "name": "Agent Bot",
    "email": "agent@example.com",
    "displayName": "Agent",
    "active": True,
}


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _task_data() -> dict[str, Any]:
    data = store().get("orders_incident")
    if data is None:
        raise RuntimeError("orders_incident store was not initialized by bootstrap()")
    return data


async def _read_branch_refs() -> dict[str, str]:
    result = await sandbox().exec(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=BARE_REPO,
    )
    if not result.success:
        raise RuntimeError(result.stderr)

    refs: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        ref, sha = parts
        if ref.startswith("refs/heads/"):
            refs[ref] = sha
    return refs


async def _detect_pushes(initial_refs: dict[str, str]) -> list[dict[str, str]]:
    pushes: list[dict[str, str]] = []
    for ref, sha in (await _read_branch_refs()).items():
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


def _resolve_issue(issue_identifier: str) -> dict[str, Any]:
    issue = _task_data()["issue"]
    if issue_identifier not in {issue.get("identifier"), issue.get("id")}:
        raise ToolError(f"Linear issue {issue_identifier!r} not found")
    return issue


@solver
def bootstrap():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        for path in sorted(ORDERS_API_DIR.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(ORDERS_API_DIR).as_posix()
            await sandbox().write_file(
                f"{WORKSPACE}/{relative}",
                path.read_text(encoding="utf-8"),
            )

        bootstrap_result = await sandbox().exec(
            [
                "bash",
                "-lc",
                f"""
                set -euo pipefail
                git init {WORKSPACE}
                cd {WORKSPACE}
                git checkout -b {DEFAULT_BRANCH}
                git config user.name "Agent Bot"
                git config user.email "agent@example.com"
                git add .
                git commit -m "Seed orders-api baseline"
                git clone --bare . ../orders-api.git
                git remote add origin ../orders-api.git
                """,
            ]
        )
        if not bootstrap_result.success:
            raise RuntimeError(bootstrap_result.stderr)

        issue = _load_json(LINEAR_ISSUES_PATH)[0]
        workflow_states = _load_json(WORKFLOW_STATES_PATH)
        store().set(
            "orders_incident",
            {
                "issue": issue,
                "done_state": next(item for item in workflow_states if item["type"] == "completed"),
                "comments": [],
                "initial_refs": await _read_branch_refs(),
            },
        )
        return state

    return solve


@tool
def get_linear_issue():
    async def execute(issue_identifier: str = "ENG-450") -> str:
        """Return the seeded Linear issue details plus any created comments.

        Args:
            issue_identifier: Issue identifier or internal id.

        Returns:
            JSON payload describing the issue.
        """
        data = _task_data()
        issue = _resolve_issue(issue_identifier)
        payload = {**issue, "created_comments": data["comments"]}
        return json.dumps(payload, indent=2)

    return execute


@tool
def leave_linear_comment():
    async def execute(body: str, issue_identifier: str = "ENG-450") -> str:
        """Leave a comment on the seeded Linear issue.

        Args:
            body: Comment body as markdown text.
            issue_identifier: Issue identifier or internal id.

        Returns:
            JSON payload for the created comment.
        """
        data = _task_data()
        issue = _resolve_issue(issue_identifier)
        comment = {
            "id": f"comment-{len(data['comments']) + 1:03d}",
            "body": body,
            "createdAt": _utc_now(),
            "user": VIEWER,
        }
        data["comments"].append(comment)
        issue["updatedAt"] = comment["createdAt"]
        return json.dumps(comment, indent=2)

    return execute


@tool
def mark_linear_done():
    async def execute(issue_identifier: str = "ENG-450") -> str:
        """Mark the seeded Linear issue as Done.

        Args:
            issue_identifier: Issue identifier or internal id.

        Returns:
            JSON payload for the new issue state.
        """
        data = _task_data()
        issue = _resolve_issue(issue_identifier)
        issue["state"] = data["done_state"]
        issue["completedAt"] = _utc_now()
        issue["updatedAt"] = issue["completedAt"]
        return json.dumps(issue["state"], indent=2)

    return execute


@scorer(metrics={"*": [mean()]})
def score_orders_incident():
    async def score(state: TaskState, target: Target) -> Score:
        del target
        data = _task_data()
        pushes = await _detect_pushes(data["initial_refs"])

        await sandbox().exec(["rm", "-rf", GRADING_DIR])
        clone_result = await sandbox().exec(["git", "clone", BARE_REPO, GRADING_DIR])
        if not clone_result.success:
            raise RuntimeError(clone_result.stderr)

        if pushes:
            grading_branch = pushes[-1]["branch"]
            grading_commit = pushes[-1]["new_sha"]
        else:
            grading_branch = DEFAULT_BRANCH
            grading_commit = (
                await sandbox().exec(
                    ["git", "rev-parse", f"refs/heads/{DEFAULT_BRANCH}"],
                    cwd=BARE_REPO,
                )
            ).stdout.strip()

        checkout_result = await sandbox().exec(["git", "checkout", grading_commit], cwd=GRADING_DIR)
        if not checkout_result.success:
            raise RuntimeError(checkout_result.stderr)
        await sandbox().write_file(
            f"{GRADING_DIR}/test_order_pricing.py",
            HIDDEN_TEST_PATH.read_text(encoding="utf-8"),
        )
        test_result = await sandbox().exec(
            ["python", "-m", "pytest", "test_order_pricing.py", "-v"],
            cwd=GRADING_DIR,
        )

        tests_pass = 1.0 if test_result.success else 0.0
        branch_pushed = 1.0 if pushes else 0.0
        linear_workflow_complete = (
            1.0 if data["issue"].get("state", {}).get("type") == "completed" and data["comments"] else 0.0
        )
        reward = (0.8 * tests_pass) + (0.1 * branch_pushed) + (0.1 * linear_workflow_complete)

        return Score(
            value={
                "reward": reward,
                "tests_pass": tests_pass,
                "branch_pushed": branch_pushed,
                "linear_workflow_complete": linear_workflow_complete,
            },
            answer=state.output.completion if state.output else "",
            explanation=json.dumps(
                {
                    "grading_branch": grading_branch,
                    "grading_commit": grading_commit,
                    "pushes": pushes,
                    "issue_state": data["issue"].get("state"),
                    "created_comment_count": len(data["comments"]),
                    "pytest_returncode": test_result.returncode,
                    "pytest_stdout_tail": test_result.stdout[-4000:],
                    "pytest_stderr_tail": test_result.stderr[-4000:],
                },
                indent=2,
            ),
        )

    return score


@task
def orders_incident():
    return Task(
        dataset=[Sample(input=PROMPT)],
        setup=bootstrap(),
        solver=react(
            tools=[
                bash_session(timeout=240),
                text_editor(timeout=180),
                get_linear_issue(),
                leave_linear_comment(),
                mark_linear_done(),
            ],
        ),
        sandbox="local",
        scorer=score_orders_incident(),
        message_limit=100,
    )
