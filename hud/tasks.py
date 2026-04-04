from __future__ import annotations

from env import orders_incident

task = orders_incident.task()
task.slug = "orders-incident"

tasks = [task]
