"""Mock Linear MCP server.

Provides a ``LinearService`` that wraps a FastMCP server with mock
Linear tools backed by pre-captured JSON data.

Usage::

    from servers.mcp.linear import LinearService

    linear = LinearService()
    env.connect_server(linear.server, prefix="linear")

    # Later, per-task:
    linear.configure(data_dir="linear_data/")

With frontend::

    linear = LinearService(frontend_port=8080)
    env.connect_server(linear.server, prefix="linear")
    # configure auto-starts frontend
    linear.configure(data_dir="linear_data/")
    # browse http://0.0.0.0:8080
"""

from __future__ import annotations

import os

from servers.mcp.base import LocalService

from .data import MockLinearData
from .frontend import LinearFrontend
from .server import create_linear_server

__all__ = [
    "LinearFrontend",
    "LinearService",
    "MockLinearData",
    "create_linear_server",
]


class LinearService(LocalService):
    """Mock Linear MCP service.

    Bundles a ``MockLinearData`` instance and a ``FastMCP`` server with
    all Linear tools. The ``.server`` property gives the FastMCP instance
    for use with ``env.connect_server()``.

    Create with no data, connect to the environment, then call
    :meth:`configure` per-task to load data::

        # In env.py (module level)
        linear_service = LinearService()
        env.connect_server(linear_service.server, prefix="linear")

        # In a task scenario
        linear_service.configure(data_dir="/mcp_server/task_data/linear_data")

    Args:
        frontend_port: Port for the read-only web frontend. If ``None``,
            falls back to ``LINEAR_FRONTEND_PORT`` env var. If neither
            is set, no frontend is created.
    """

    def __init__(self, frontend_port: int | None = None) -> None:
        super().__init__()
        self.data = MockLinearData()

        detach = False
        if frontend_port is None:
            env_port = os.environ.get("LINEAR_FRONTEND_PORT")
            if env_port:
                frontend_port = int(env_port)
                detach = True

        self.frontend: LinearFrontend | None = (
            LinearFrontend(self.data, port=frontend_port, detach=detach) if frontend_port else None
        )

    def configure(self, *, data_dir: str) -> None:
        """Load or reload Linear mock data from the given directory.

        Resets all in-memory mutations from previous tasks.
        Auto-starts the frontend if a port was configured.
        """
        self.data.reload(data_dir)
        self.start_frontend()

    def start_frontend(self) -> None:
        """Start the web frontend if configured. Idempotent."""
        if self.frontend is not None:
            self.frontend.start()

    def stop_frontend(self) -> None:
        """Stop the web frontend if running. Idempotent."""
        if self.frontend is not None:
            self.frontend.stop()

    def _create_server(self):
        return create_linear_server(self.data)
