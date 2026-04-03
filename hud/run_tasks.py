"""Run the local HUD environment against the v5 task definitions."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import hud
from hud.agents import OpenAIChatAgent


PROJECT_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("ORDERS_RUNTIME_ROOT", str(PROJECT_ROOT / ".runtime" / "run_tasks"))
os.environ.setdefault("ORDERS_CHOWN_WORKSPACE", "0")

from tasks import tasks


async def main() -> None:
    async with hud.eval(tasks) as ctx:
        agent = OpenAIChatAgent.create(model="gpt-4o")
        await agent.run(ctx, max_steps=30)


if __name__ == "__main__":
    asyncio.run(main())
