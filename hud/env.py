"""HUD implementation of the orders/Linear incident scenario."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from hud import Environment

from runtime import OrdersIncidentRuntime
from task_catalog import LINEAR_EXAMPLE

ROOT_DIR = Path(__file__).resolve().parent
PYPROJECT_PATH = ROOT_DIR / "pyproject.toml"


def _default_env_name() -> str:
    if PYPROJECT_PATH.is_file():
        pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
        hud_image = ((pyproject.get("tool") or {}).get("hud") or {}).get("image")
        if isinstance(hud_image, str) and hud_image:
            return hud_image.split(":", 1)[0]
        project_name = (pyproject.get("project") or {}).get("name")
        if isinstance(project_name, str) and project_name:
            return project_name
    return ROOT_DIR.name


ENV_NAME = os.environ.get("HUD_ENV_NAME", _default_env_name())

env = Environment(ENV_NAME)
runtime = OrdersIncidentRuntime(ROOT_DIR)
runtime.attach(env, LINEAR_EXAMPLE)


@env.scenario(LINEAR_EXAMPLE.scenario)
async def linear_example():
    answer = yield await runtime.setup(LINEAR_EXAMPLE)
    yield await runtime.grade(LINEAR_EXAMPLE, answer)


if __name__ == "__main__":
    env.run(transport="stdio")
