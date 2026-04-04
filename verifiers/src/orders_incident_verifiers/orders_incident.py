from __future__ import annotations

import asyncio
import importlib.resources as resources
import json
import shutil
import subprocess
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import verifiers as vf
from datasets import Dataset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = resources.files("orders_incident_verifiers") / "assets"
ORDERS_API_ASSETS = ASSETS_DIR / "orders_api"
HIDDEN_TEST_RESOURCE = ASSETS_DIR / "tests" / "test_order_pricing.py"
LINEAR_ISSUES_RESOURCE = ASSETS_DIR / "linear_data" / "issues.json"
WORKFLOW_STATES_RESOURCE = ASSETS_DIR / "linear_data" / "workflow_states.json"
RUNTIME_BASE = PROJECT_ROOT / ".runtime" / "rollouts"
DEFAULT_BRANCH = "order_bug_baseline"
ENV_ID = "orders-incident-verifiers"
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
VIEWER = {
    "id": "user-001",
    "name": "Agent Bot",
    "email": "agent@example.com",
    "displayName": "Agent",
    "active": True,
}


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _load_json_resource(resource: Any) -> Any:
    return json.loads(resource.read_text(encoding="utf-8"))


def _copy_traversable_tree(source: Any, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        child_dest = dest / child.name
        if child.is_dir():
            _copy_traversable_tree(child, child_dest)
        else:
            child_dest.write_text(child.read_text(encoding="utf-8"), encoding="utf-8")


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


class OrdersIncidentVerifiersEnv(vf.StatefulToolEnv):
    def __init__(self, *, keep_runtime: bool = False, max_turns: int = 30):
        self.keep_runtime = keep_runtime
        dataset = Dataset.from_list(
            [
                {
                    "prompt": [{"role": "user", "content": "Preparing workspace..."}],
                    "info": {"task_slug": "orders-incident"},
                }
            ]
        )
        rubric = vf.Rubric()
        rubric.add_reward_func(self.tests_pass, weight=0.8)
        rubric.add_reward_func(self.branch_pushed, weight=0.1)
        rubric.add_reward_func(self.linear_workflow_complete, weight=0.1)
        super().__init__(
            dataset=dataset,
            eval_dataset=dataset,
            rubric=rubric,
            max_turns=max_turns,
            env_id=ENV_ID,
        )
        self.add_tool(self.get_linear_issue, args_to_skip=["state"])
        self.add_tool(self.list_files, args_to_skip=["state"])
        self.add_tool(self.read_file, args_to_skip=["state"])
        self.add_tool(self.write_file, args_to_skip=["state"])
        self.add_tool(self.replace_in_file, args_to_skip=["state"])
        self.add_tool(self.run_command, args_to_skip=["state"])
        self.add_tool(self.leave_linear_comment, args_to_skip=["state"])
        self.add_tool(self.mark_linear_done, args_to_skip=["state"])

    async def setup_state(self, state: vf.State) -> vf.State:
        await asyncio.to_thread(self._prepare_rollout_state, state)
        state["prompt"] = [
            {
                "role": "user",
                "content": PROMPT_TEMPLATE.format(workspace=state["workspace"]),
            }
        ]
        state["completion"] = []
        return await super().setup_state(state)

    def update_tool_args(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        messages: vf.Messages,
        state: vf.State,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del messages, kwargs
        if "state" in self.skipped_args.get(tool_name, []):
            tool_args["state"] = state
        return tool_args

    async def tests_pass(self, state: vf.State) -> float:
        grading = await self._ensure_grading(state)
        return float(grading["test_score"])

    async def branch_pushed(self, state: vf.State) -> float:
        grading = await self._ensure_grading(state)
        return 1.0 if grading["pushes"] else 0.0

    async def linear_workflow_complete(self, state: vf.State) -> float:
        issue_state = (state["linear_issue"].get("state") or {})
        comments = state.get("linear_comments") or []
        return 1.0 if issue_state.get("type") == "completed" and comments else 0.0

    async def get_linear_issue(
        self,
        issue_identifier: str = "ENG-450",
        state: vf.State | None = None,
    ) -> str:
        """Return the seeded Linear issue details plus any created comments."""
        assert state is not None
        issue = state["linear_issue"]
        if issue_identifier not in {issue["identifier"], issue["id"]}:
            return json.dumps({"error": f"Linear issue {issue_identifier!r} not found"}, indent=2)

        payload = deepcopy(issue)
        payload["created_comments"] = deepcopy(state.get("linear_comments", []))
        return json.dumps(payload, indent=2)

    async def list_files(self, path: str = ".", state: vf.State | None = None) -> str:
        """List files in the working copy relative to the repo root."""
        assert state is not None
        workspace = Path(state["workspace"])
        target = self._resolve_workspace_path(workspace, path)
        if not target.exists():
            return f"Path not found: {target}"
        if target.is_file():
            return str(target.relative_to(workspace))

        entries: list[str] = []
        for child in sorted(target.rglob("*")):
            if child.is_dir() and child.name == ".git":
                continue
            if ".git" in child.parts:
                continue
            if child.is_file():
                entries.append(str(child.relative_to(workspace)))
        return "\n".join(entries)

    async def read_file(self, path: str, state: vf.State | None = None) -> str:
        """Read a file from the repo."""
        assert state is not None
        workspace = Path(state["workspace"])
        target = self._resolve_workspace_path(workspace, path)
        return target.read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str, state: vf.State | None = None) -> str:
        """Write a full file inside the repo."""
        assert state is not None
        workspace = Path(state["workspace"])
        target = self._resolve_workspace_path(workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {target.relative_to(workspace)}"

    async def replace_in_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        state: vf.State | None = None,
    ) -> str:
        """Replace the first occurrence of text in a repo file."""
        assert state is not None
        workspace = Path(state["workspace"])
        target = self._resolve_workspace_path(workspace, path)
        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Text not found in {target.relative_to(workspace)}"
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Updated {target.relative_to(workspace)}"

    async def run_command(
        self,
        command: str,
        timeout_seconds: int = 30,
        state: vf.State | None = None,
    ) -> str:
        """Run a shell command from the repo root."""
        assert state is not None
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=state["workspace"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return f"Command timed out after {timeout_seconds}s"

        stdout_text = stdout.decode()
        stderr_text = stderr.decode()
        output = [f"exit_code: {process.returncode}"]
        if stdout_text:
            output.append("stdout:\n" + stdout_text[-4000:])
        if stderr_text:
            output.append("stderr:\n" + stderr_text[-4000:])
        return "\n\n".join(output)

    async def leave_linear_comment(self, body: str, state: vf.State | None = None) -> str:
        """Add a comment to ENG-450."""
        assert state is not None
        comment = {
            "id": str(uuid.uuid4()),
            "body": body,
            "createdAt": _utc_now(),
            "user": deepcopy(VIEWER),
        }
        state["linear_comments"].append(comment)
        state["linear_issue"]["updatedAt"] = comment["createdAt"]
        return json.dumps(comment, indent=2)

    async def mark_linear_done(self, state: vf.State | None = None) -> str:
        """Mark ENG-450 as Done."""
        assert state is not None
        completed_state = deepcopy(
            next(
                workflow
                for workflow in state["workflow_states"]
                if workflow.get("type") == "completed"
            )
        )
        state["linear_issue"]["state"] = completed_state
        state["linear_issue"]["completedAt"] = _utc_now()
        state["linear_issue"]["updatedAt"] = state["linear_issue"]["completedAt"]
        return json.dumps(
            {
                "id": completed_state["id"],
                "name": completed_state["name"],
                "type": completed_state["type"],
            },
            indent=2,
        )

    def _prepare_rollout_state(self, state: vf.State) -> None:
        runtime_root = RUNTIME_BASE / state["trajectory_id"]
        workspace = runtime_root / "workspace" / "orders_api"
        bare_repo = runtime_root / "git" / "orders-api.git"
        grading_dir = runtime_root / "grading" / "orders_api"

        shutil.rmtree(runtime_root, ignore_errors=True)
        for path in [workspace, bare_repo, grading_dir]:
            path.parent.mkdir(parents=True, exist_ok=True)

        _copy_traversable_tree(ORDERS_API_ASSETS, workspace)
        subprocess.run(["git", "init"], cwd=workspace, check=True)
        subprocess.run(["git", "checkout", "-b", DEFAULT_BRANCH], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.name", "Agent Bot"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-m", "Seed orders-api baseline"], cwd=workspace, check=True)
        subprocess.run(["git", "clone", "--bare", str(workspace), str(bare_repo)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare_repo)], cwd=workspace, check=True)

        state["runtime_root"] = str(runtime_root)
        state["workspace"] = str(workspace)
        state["bare_repo"] = str(bare_repo)
        state["grading_dir"] = str(grading_dir)
        state["initial_refs"] = _snapshot_refs(bare_repo)
        state["linear_issue"] = deepcopy(_load_json_resource(LINEAR_ISSUES_RESOURCE)[0])
        state["workflow_states"] = deepcopy(_load_json_resource(WORKFLOW_STATES_RESOURCE))
        state["linear_comments"] = []
        state["grading_result"] = None

    async def _ensure_grading(self, state: vf.State) -> dict[str, Any]:
        cached = state.get("grading_result")
        if cached is not None:
            return cached
        grading = await asyncio.to_thread(self._grade_submission_sync, state)
        state["grading_result"] = grading
        if not self.keep_runtime:
            runtime_root = state.get("runtime_root")
            if runtime_root:
                await asyncio.to_thread(shutil.rmtree, runtime_root, True)
        return grading

    def _grade_submission_sync(self, state: vf.State) -> dict[str, Any]:
        bare_repo = Path(state["bare_repo"])
        grading_dir = Path(state["grading_dir"])
        pushes = _detect_pushes(bare_repo, state["initial_refs"])

        shutil.rmtree(grading_dir, ignore_errors=True)
        grading_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", str(bare_repo), str(grading_dir)], check=True)

        if pushes:
            grading_branch = pushes[-1]["branch"]
            grading_commit = pushes[-1]["new_sha"]
        else:
            grading_branch = DEFAULT_BRANCH
            grading_commit = subprocess.run(
                ["git", "rev-parse", f"refs/heads/{DEFAULT_BRANCH}"],
                cwd=bare_repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        subprocess.run(["git", "checkout", grading_commit], cwd=grading_dir, check=True)
        (grading_dir / HIDDEN_TEST_RESOURCE.name).write_text(
            HIDDEN_TEST_RESOURCE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        result = subprocess.run(
            ["python", "-m", "pytest", HIDDEN_TEST_RESOURCE.name, "-v"],
            cwd=grading_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "test_score": 1.0 if result.returncode == 0 else 0.0,
            "pushes": pushes,
            "grading_branch": grading_branch,
            "grading_commit": grading_commit,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
            "exit_code": result.returncode,
        }

    def _resolve_workspace_path(self, workspace: Path, path: str) -> Path:
        raw = Path(path)
        target = raw if raw.is_absolute() else workspace / raw
        target = target.resolve()
        if not target.is_relative_to(workspace.resolve()):
            raise ValueError(f"Path escapes workspace: {path}")
        return target


def load_environment(*, keep_runtime: bool = False, max_turns: int = 30) -> vf.Environment:
    return OrdersIncidentVerifiersEnv(keep_runtime=keep_runtime, max_turns=max_turns)


async def _smoke() -> None:
    env = load_environment(keep_runtime=False)
    row = env.get_eval_dataset()[0]
    state = await env.init_state(row, client=None, model="dry-run")
    state = await env.setup_state(state)
    issue = await env.get_linear_issue(state=state)
    await env.rubric.score_rollout(state)
    print("workspace:", state["workspace"])
    print("linear_issue:", json.loads(issue)["identifier"])
    print("reward:", state["reward"])
    print("metrics:", state["metrics"])
    print("grading_branch:", state["grading_result"]["grading_branch"])
    print("grading_commit:", state["grading_result"]["grading_commit"])
    await env._cleanup(state)


def main() -> None:
    asyncio.run(_smoke())
