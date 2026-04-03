from __future__ import annotations

from env import linear_example
from task_catalog import TASK_SPECS

tasks = []

for spec in TASK_SPECS:
    task = linear_example.task()
    task.slug = spec.slug
    task.metadata["task_id"] = spec.task_id
    task.metadata["repo_owner"] = spec.repo_owner
    task.metadata["repo_name"] = spec.repo_name
    task.metadata["linear_issue"] = spec.linear_issue
    tasks.append(task)
