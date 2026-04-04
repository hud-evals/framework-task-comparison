"""Minimal HUD environment for the orders incident demo."""

from __future__ import annotations

import platform
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


class OrdersIncidentRuntime:
    def __init__(self) -> None:
        in_docker = Path("/.dockerenv").exists()
        runtime_root = Path("/home/ubuntu" if in_docker else ROOT_DIR / ".runtime" / "dev")
        self.workspace = runtime_root / "workspace" / "orders_api"
        self.bare_repo = runtime_root / "git" / "orders-api.git"
        self.grading_dir = runtime_root / "grading" / "orders_api"
        self.chown_workspace = in_docker and platform.system() == "Linux"
        self.linear = LinearService()
        self.initial_refs: dict[str, str] = {}

    def attach(self, env: Environment) -> None:
        env.add_tool(BashTool())
        env.add_tool(EditTool())
        env.add_tool(ReadTool(base_path=str(self.workspace)))
        env.add_tool(GrepTool(base_path=str(self.workspace)))
        env.add_tool(GlobTool(base_path=str(self.workspace)))
        env.add_tool(ListTool(base_path=str(self.workspace)))
        env.connect_server(self.linear.server, prefix="linear")

    async def setup(self) -> str:
        for path in [self.workspace, self.bare_repo, self.grading_dir]:
            shutil.rmtree(path, ignore_errors=True)
            path.parent.mkdir(parents=True, exist_ok=True)

        shutil.copytree(ROOT_DIR / "source" / "orders_api", self.workspace)
        self._git(self.workspace, "init")
        self._git(self.workspace, "checkout", "-b", "order_bug_baseline")
        self._git(self.workspace, "config", "user.name", "Agent Bot")
        self._git(self.workspace, "config", "user.email", "agent@example.com")
        self._git(self.workspace, "add", ".")
        self._git(self.workspace, "commit", "-m", "Seed orders-api baseline")
        self._run(["git", "clone", "--bare", str(self.workspace), str(self.bare_repo)])
        self.initial_refs = _snapshot_refs(self.bare_repo)
        self._git(self.workspace, "remote", "add", "origin", str(self.bare_repo))
        self.linear.configure(data_dir=str(ROOT_DIR / "linear_data"))

        if self.chown_workspace:
            subprocess.run(["chown", "-R", "1000:1000", str(self.workspace.parent)], check=False)

        return PROMPT.format(workspace=self.workspace)

    async def grade(self, answer: Any) -> EvaluationResult:
        pushes = _detect_pushes(self.bare_repo, self.initial_refs)

        shutil.rmtree(self.grading_dir, ignore_errors=True)
        self._run(["git", "clone", str(self.bare_repo), str(self.grading_dir)])

        grading_branch = pushes[-1]["branch"] if pushes else "order_bug_baseline"
        grading_commit = pushes[-1]["new_sha"] if pushes else self._git(
            self.bare_repo, "rev-parse", "refs/heads/order_bug_baseline"
        ).stdout.strip()

        self._git(self.grading_dir, "checkout", grading_commit)
        (self.grading_dir / "test_order_pricing.py").write_text(
            (ROOT_DIR / "source" / "tests" / "test_order_pricing.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        test_result = self._run(
            ["python", "-m", "pytest", "test_order_pricing.py", "-v"],
            cwd=self.grading_dir,
            check=False,
        )
        issue = self.linear.data.get_issue("ENG-450")
        issue_state = (issue or {}).get("state") or {}
        comments = (getattr(self.linear.data, "_created_comments", {}) or {}).get("issue-450", [])

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
                "workspace": str(self.workspace),
                "agent_answer_preview": str(answer)[:500] if answer is not None else "",
            },
        )

    def _git(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(["git", *args], cwd=cwd, check=check)

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
        if result.returncode != 0 and check:
            raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
        return result


env = Environment("orders-incident-hud")
runtime = OrdersIncidentRuntime()
runtime.attach(env)


@env.scenario("orders_incident")
async def orders_incident():
    answer = yield await runtime.setup()
    yield await runtime.grade(answer)


if __name__ == "__main__":
    env.run(transport="stdio")
