from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hud import Environment
from hud.tools import BashTool, EditTool
from hud.tools.filesystem import GlobTool, GrepTool, ListTool, ReadTool
from hud.tools.types import EvaluationResult, SubScore

from servers.mcp.github import MockGitHubService
from servers.mcp.linear import LinearService
from task_catalog import OrdersIncidentTaskSpec, render_prompt

logger = logging.getLogger(__name__)


def _default_runtime_root(project_root: Path) -> Path:
    if Path("/.dockerenv").exists():
        return Path("/home/ubuntu")
    return project_root / ".runtime" / "dev"


class OrdersIncidentRuntime:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.runtime_root = Path(
            os.environ.get(
                "ORDERS_RUNTIME_ROOT",
                str(_default_runtime_root(project_root)),
            )
        )
        self.workspace_base = Path(
            os.environ.get(
                "ORDERS_WORKSPACE_BASE",
                str(self.runtime_root / "workspace"),
            )
        )
        self.chown_workspace = os.environ.get(
            "ORDERS_CHOWN_WORKSPACE",
            "1" if Path("/.dockerenv").exists() and platform.system() == "Linux" else "0",
        ) == "1"

        self.github_service = MockGitHubService()
        self.linear_service = LinearService()

    def attach(self, env: Environment, task: OrdersIncidentTaskSpec) -> None:
        workspace = self.workspace(task)

        env.add_tool(BashTool())
        env.add_tool(EditTool())
        env.add_tool(ReadTool(base_path=str(workspace)))
        env.add_tool(GrepTool(base_path=str(workspace)))
        env.add_tool(GlobTool(base_path=str(workspace)))
        env.add_tool(ListTool(base_path=str(workspace)))

        env.connect_server(self.github_service.server, prefix="github")
        env.connect_server(self.linear_service.server, prefix="linear")

    def workspace(self, task: OrdersIncidentTaskSpec) -> Path:
        return self.workspace_base / task.workspace_name

    def workspace_display(self, task: OrdersIncidentTaskSpec) -> str:
        return os.environ.get("ORDERS_WORKSPACE_DISPLAY", str(self.workspace(task)))

    def bare_repo(self, task: OrdersIncidentTaskSpec) -> Path:
        default = self.runtime_root / "git" / f"{task.repo_name}.git"
        return Path(os.environ.get("ORDERS_BARE_REPO", str(default)))

    def seed_repo(self, task: OrdersIncidentTaskSpec) -> Path:
        default = self.runtime_root / "seed" / task.repo_name
        return Path(os.environ.get("ORDERS_SEED_REPO", str(default)))

    def grading_dir(self, task: OrdersIncidentTaskSpec) -> Path:
        default = self.runtime_root / "grading" / task.workspace_name
        return Path(os.environ.get("ORDERS_GRADING_DIR", str(default)))

    def source_dir(self, task: OrdersIncidentTaskSpec) -> Path:
        return self.project_root / task.source_dir

    def test_path(self, task: OrdersIncidentTaskSpec) -> Path:
        return self.project_root / task.test_path

    def github_data_dir(self, task: OrdersIncidentTaskSpec) -> Path:
        return self.project_root / "data" / task.task_data_dir / task.github_data_subdir

    def linear_data_dir(self, task: OrdersIncidentTaskSpec) -> Path:
        return self.project_root / "data" / task.task_data_dir / task.linear_data_subdir

    async def setup(self, task: OrdersIncidentTaskSpec) -> str:
        self._reset_environment(task)
        self._prepare_workspace(task)
        return render_prompt(task, self.workspace_display(task))

    async def grade(
        self,
        task: OrdersIncidentTaskSpec,
        answer: Any,
    ) -> EvaluationResult:
        grading_dir = self.grading_dir(task)
        bare_repo = self.bare_repo(task)
        pushes = self.github_service.client.detect_pushes()

        shutil.rmtree(grading_dir, ignore_errors=True)
        self._run(f"mkdir -p {self._quote(grading_dir.parent)}")
        self._run(f"git clone {self._quote(bare_repo)} {self._quote(grading_dir)}")

        grading_branch = self._select_grading_branch(task, pushes)
        self._run(f"git -C {self._quote(grading_dir)} checkout {grading_branch}")
        self._write_hidden_test_file(task, grading_dir)

        test_score, test_meta = self._run_hidden_tests(task, grading_dir)
        pull_requests = await self.github_service.client.list_pull_requests(
            task.repo_owner,
            task.repo_name,
            state="all",
            per_page=50,
            page=1,
        )
        issue = self.linear_service.data.get_issue(task.linear_issue)
        issue_state = (issue or {}).get("state") or {}
        created_comments = (
            getattr(self.linear_service.data, "_created_comments", {}) or {}
        ).get(task.linear_issue_id, [])

        subscores = [
            SubScore(
                name="tests_pass",
                weight=0.7,
                value=test_score,
                metadata=test_meta,
            ),
            SubScore(
                name="branch_pushed",
                weight=0.1,
                value=1.0 if pushes else 0.0,
                metadata={"pushes": pushes},
            ),
            SubScore(
                name="pull_request_created",
                weight=0.1,
                value=1.0 if pull_requests else 0.0,
                metadata={"pull_request_numbers": [pr.get("number") for pr in pull_requests]},
            ),
            SubScore(
                name="linear_workflow_complete",
                weight=0.1,
                value=1.0 if issue_state.get("type") == "completed" and created_comments else 0.0,
                metadata={
                    "linear_state": issue_state,
                    "created_comment_count": len(created_comments),
                },
            ),
        ]
        reward = sum(item.weight * item.value for item in subscores)

        return EvaluationResult(
            reward=reward,
            done=True,
            content=(
                "Tests, git push/PR workflow, and Linear issue actions were checked "
                "against the local mock services."
            ),
            subscores=subscores,
            info={
                "grading_branch": grading_branch,
                "workspace": str(self.workspace(task)),
                "source_directory": str(self.source_dir(task)),
                "agent_answer_preview": str(answer)[:500] if answer is not None else "",
            },
        )

    def _quote(self, value: Path | str) -> str:
        return shlex.quote(str(value))

    def _run(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        logger.info("bash: %s", command)
        result = subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Command failed (%d)\n--- stdout ---\n%s\n--- stderr ---\n%s",
                result.returncode,
                result.stdout[-4000:] if result.stdout else "(empty)",
                result.stderr[-4000:] if result.stderr else "(empty)",
            )
            if check:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    command,
                    result.stdout,
                    result.stderr,
                )
        return result

    def _reset_environment(self, task: OrdersIncidentTaskSpec) -> None:
        for path in [
            self.workspace(task),
            self.bare_repo(task),
            self.seed_repo(task),
            self.grading_dir(task),
        ]:
            shutil.rmtree(path, ignore_errors=True)

        self.github_service._clients.clear()
        self.github_service._register_client(self.github_service.client)

        if getattr(self.linear_service.data, "_loaded", False):
            self.linear_service.data.reload(str(self.linear_data_dir(task)))

    def _prepare_workspace(self, task: OrdersIncidentTaskSpec) -> None:
        workspace = self.workspace(task)
        bare_repo = self.bare_repo(task)
        seed_repo = self.seed_repo(task)

        self.workspace_base.mkdir(parents=True, exist_ok=True)
        bare_repo.parent.mkdir(parents=True, exist_ok=True)
        seed_repo.parent.mkdir(parents=True, exist_ok=True)

        self.github_service.configure(
            bare_repo_path=str(bare_repo),
            data_dir=str(self.github_data_dir(task)),
            repo_owner=task.repo_owner,
            repo_name=task.repo_name,
            default_branch=task.baseline_branch,
            worktree_path=str(workspace),
        )
        self._initialize_seed_repo(task, seed_repo)
        self._run(f"git clone --bare {self._quote(seed_repo)} {self._quote(bare_repo)}")
        self.github_service.client.snapshot_refs()

        self.linear_service.configure(data_dir=str(self.linear_data_dir(task)))

        self._run(f"rm -rf {self._quote(workspace)}")
        self._run(
            f"git clone --branch {task.baseline_branch} "
            f"{self._quote(bare_repo)} {self._quote(workspace)}"
        )
        self._run(
            f"git -C {self._quote(workspace)} config user.name 'Agent Bot' && "
            f"git -C {self._quote(workspace)} config user.email 'agent@example.com'"
        )
        if self.chown_workspace:
            self._run(f"chown -R 1000:1000 {self._quote(self.workspace_base)}", check=False)

    def _initialize_seed_repo(self, task: OrdersIncidentTaskSpec, seed_repo: Path) -> None:
        source_dir = self.source_dir(task)
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Missing source fixture directory: {source_dir}")

        shutil.copytree(source_dir, seed_repo)
        self._run(f"git init {self._quote(seed_repo)}")
        self._run(f"git -C {self._quote(seed_repo)} checkout -b {task.baseline_branch}")
        self._run(
            f"git -C {self._quote(seed_repo)} config user.name 'Seed Repo' && "
            f"git -C {self._quote(seed_repo)} config user.email 'seed@example.com'"
        )
        self._run(f"git -C {self._quote(seed_repo)} add .")
        self._run(f"git -C {self._quote(seed_repo)} commit -m 'Seed orders-api baseline'")

    def _write_hidden_test_file(self, task: OrdersIncidentTaskSpec, target_dir: Path) -> None:
        (target_dir / task.hidden_test_file).write_text(
            self.test_path(task).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def _select_grading_branch(
        self,
        task: OrdersIncidentTaskSpec,
        pushes: list[dict[str, str]],
    ) -> str:
        if pushes:
            return pushes[-1]["branch"]
        return task.baseline_branch

    def _run_hidden_tests(
        self,
        task: OrdersIncidentTaskSpec,
        target_dir: Path,
    ) -> tuple[float, dict[str, Any]]:
        command = f"cd {self._quote(target_dir)} && python -m pytest {task.hidden_test_file} -v"
        result = self._run(command, check=False)
        return (
            1.0 if result.returncode == 0 else 0.0,
            {
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            },
        )
