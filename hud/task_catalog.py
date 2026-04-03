from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TaskId = Literal["linear_example"]


@dataclass(frozen=True)
class OrdersIncidentTaskSpec:
    task_id: TaskId
    slug: str
    scenario: str
    prompt_template: str
    repo_owner: str
    repo_name: str
    workspace_name: str
    linear_issue: str
    linear_issue_id: str
    baseline_branch: str
    source_dir: str
    test_path: str
    hidden_test_file: str
    task_data_dir: str
    github_data_subdir: str
    linear_data_subdir: str


LINEAR_EXAMPLE = OrdersIncidentTaskSpec(
    task_id="linear_example",
    slug="linear-example",
    scenario="linear_example",
    prompt_template=(
        "You have been assigned Linear issue ENG-450. Start in Linear and read the ticket details, "
        "including any linked GitHub issues.\n\n"
        "The orders API source code is available locally at {workspace} and "
        "on GitHub (owner: {repo_owner}, repo: {repo_name}). It is a Python FastAPI application.\n\n"
        "This looks like a production regression from last night's deploy. Customers are reporting "
        "bad checkout totals, but the ticket has the best context on what is and is not broken. "
        "Use that context to diagnose the issue and fix the root cause.\n\n"
        "Once fixed:\n"
        "1. Commit your changes to a new branch\n"
        "2. Push the branch to origin\n"
        "3. Create a pull request using the GitHub tools\n"
        "4. Leave a comment on the Linear issue summarizing the diagnosis and fix\n"
        "5. Mark the Linear issue as Done\n"
    ),
    repo_owner="acme-corp",
    repo_name="orders-api",
    workspace_name="orders_api",
    linear_issue="ENG-450",
    linear_issue_id="issue-450",
    baseline_branch="order_bug_baseline",
    source_dir="source/orders_api",
    test_path="source/tests/test_order_pricing.py",
    hidden_test_file="test_order_pricing.py",
    task_data_dir="linear_example_task",
    github_data_subdir="orders_github_data",
    linear_data_subdir="orders_linear_data",
)

TASK_SPECS = [LINEAR_EXAMPLE]
TASK_SPECS_BY_ID = {task.task_id: task for task in TASK_SPECS}


def render_prompt(task: OrdersIncidentTaskSpec, workspace: str) -> str:
    return task.prompt_template.format(
        workspace=workspace,
        repo_owner=task.repo_owner,
        repo_name=task.repo_name,
    )
