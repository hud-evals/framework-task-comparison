"""Mock Linear MCP service used by the demo."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from typing import Any

from fastmcp import FastMCP

from .data import MockLinearData
from .server import create_linear_server

__all__ = [
    "LinearService",
    "MockLinearData",
    "create_linear_server",
]


def _run_awaitable(coro: object) -> FastMCP:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()  # type: ignore[arg-type]


class LocalService(ABC):
    def __init__(self) -> None:
        self._server: FastMCP | None = None

    @abstractmethod
    def _create_server(self) -> FastMCP | Coroutine[Any, Any, FastMCP]:
        ...

    @property
    def server(self) -> FastMCP:
        if self._server is None:
            result = self._create_server()
            self._server = _run_awaitable(result) if inspect.isawaitable(result) else result
        return self._server


class LinearService(LocalService):
    """Small wrapper around the mock Linear data + server."""

    def __init__(self) -> None:
        super().__init__()
        self.data = MockLinearData()

    def configure(self, *, data_dir: str) -> None:
        self.data.reload(data_dir)

    def _create_server(self):
        return create_linear_server(self.data)
