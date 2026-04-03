"""Base class for local MCP services.

All mock MCP services (Sentry, Linear, GitHub, etc.) extend this
to ensure a consistent interface for consumers.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from typing import Any

from fastmcp import FastMCP


def _run_awaitable(coro: object) -> FastMCP:
    """Resolve an awaitable to a ``FastMCP`` instance.

    Uses ``asyncio.run()`` when no event loop is active, otherwise
    runs in a worker thread to avoid nesting loops.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()  # type: ignore[arg-type]


class LocalService(ABC):
    """Base class for local MCP services.

    Subclasses must implement ``_create_server()`` to return a
    ``FastMCP`` instance with their tools registered. The server
    is created lazily on first access via ``.server``.

    ``_create_server`` may be a regular function returning ``FastMCP``
    or an ``async def`` returning a coroutine — the property handles
    both transparently.

    Usage::

        sentry = SentryService(data_dir="sentry_data/")
        env.connect_server(sentry.server, prefix="sentry")
    """

    def __init__(self) -> None:
        self._server: FastMCP | None = None

    @abstractmethod
    def _create_server(self) -> FastMCP | Coroutine[Any, Any, FastMCP]:
        """Create and return the FastMCP server instance."""
        ...

    @property
    def server(self) -> FastMCP:
        """The FastMCP server instance (created lazily)."""
        if self._server is None:
            result = self._create_server()
            if inspect.isawaitable(result):
                self._server = _run_awaitable(result)
            else:
                self._server = result
        return self._server
