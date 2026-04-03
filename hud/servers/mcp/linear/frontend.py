"""Read-only web frontend for mock Linear data.

Serves a lightweight HTML UI backed by the same ``MockLinearData``
instance used by the MCP tools.  Runs in a background daemon thread
using stdlib ``http.server``.

Templates and static assets live in ``ui/``.  Templates use
``{{placeholder}}`` syntax, substituted via single-pass ``re.sub``.

Usage::

    from servers.mcp.linear.frontend import LinearFrontend

    frontend = LinearFrontend(data, port=8080)
    frontend.start()
    # browse http://localhost:8080
    frontend.stop()
"""

from __future__ import annotations

import html
import logging
import mimetypes
import os
import re
import signal
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

if TYPE_CHECKING:
    from .data import MockLinearData

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).parent / "ui"

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

PRIORITY_COLORS = {
    1: "#eb5757",  # urgent
    2: "#f2994a",  # high
    3: "#5e6ad2",  # medium
    4: "#95a2b3",  # low
}

PRIORITY_LABELS = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}

STATE_TYPE_COLORS = {
    "backlog": "#bec2c8",
    "unstarted": "#e2e2e2",
    "started": "#f2c94c",
    "completed": "#4cb782",
    "canceled": "#95a2b3",
}

# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _render(name: str, **kwargs: str) -> str:
    """Load a template file and substitute ``{{placeholders}}`` in one pass."""
    template = (_UI_DIR / name).read_text()

    def replacer(m: re.Match[str]) -> str:
        key = m.group(1)
        assert isinstance(key, str)
        return kwargs.get(key, m.group(0))

    return _PLACEHOLDER_RE.sub(replacer, template)


def _pct(raw: float | int) -> int:
    """Normalize progress to 0-100 int. Data uses 0..1 ratios."""
    if isinstance(raw, int | float) and raw <= 1:
        return int(round(raw * 100))
    return int(round(raw))


def _e(text: Any) -> str:
    """Escape text for HTML output."""
    return html.escape(str(text)) if text else ""


def _state_color(state: dict[str, Any] | None) -> str:
    if not state:
        return STATE_TYPE_COLORS.get("backlog", "#bec2c8")
    return state.get("color") or STATE_TYPE_COLORS.get(state.get("type", ""), "#bec2c8")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class _LinearHandler(BaseHTTPRequestHandler):
    data: MockLinearData

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("LinearFrontend: %s", format % args)

    # -- response helpers ---------------------------------------------------

    def _send(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _page(self, title: str, content: str, active: str = "") -> str:
        def _cls(name: str) -> str:
            return ' class="active"' if name == active else ""

        return _render(
            "layout.html",
            title=_e(title),
            dashboard_cls=_cls("dashboard"),
            issues_cls=_cls("issues"),
            projects_cls=_cls("projects"),
            content=content,
        )

    def _error_page(self, status: int, message: str) -> None:
        body = self._page(str(status), f"<h1>{_e(message)}</h1>")
        self._send(status, body)

    def _serve_static(self, path: str) -> None:
        try:
            resolved = (_UI_DIR / path).resolve()
            if not str(resolved).startswith(str(_UI_DIR.resolve())):
                self._error_page(403, "Forbidden")
                return
            if not resolved.is_file():
                self._error_page(404, "Static file not found")
                return
            data = resolved.read_bytes()
            ctype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
            self._send_bytes(200, data, ctype)
        except (OSError, ValueError):
            self._error_page(404, "Static file not found")

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]

        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return

        if path == "/":
            self._serve_dashboard()
        elif parts == ["issues"]:
            self._serve_issue_list()
        elif len(parts) == 2 and parts[0] == "issues":
            self._serve_issue_detail(unquote(parts[1]))
        elif parts == ["projects"]:
            self._serve_project_list()
        elif len(parts) == 2 and parts[0] == "projects":
            self._serve_project_detail(unquote(parts[1]))
        else:
            self._error_page(404, "Not Found")

    # -- pages -------------------------------------------------------------

    def _serve_dashboard(self) -> None:
        data = self.data
        issues = data.all_issues()
        projects = data.all_projects()
        body = f"""
<h1>Linear (mock)</h1>
<div class="counts">
  <div class="count-card"><a class="card-link" href="/issues"><div class="num">{len(issues)}</div><div class="lbl">Issues</div></a></div>
  <div class="count-card"><a class="card-link" href="/projects"><div class="num">{len(projects)}</div><div class="lbl">Projects</div></a></div>
  <div class="count-card"><div class="num">{len(data.list_teams())}</div><div class="lbl">Teams</div></div>
  <div class="count-card"><div class="num">{len(data.users)}</div><div class="lbl">Users</div></div>
</div>"""
        self._send(200, self._page("Dashboard", body, active="dashboard"))

    def _serve_issue_list(self) -> None:
        issues = self.data.all_issues()
        rows = ""
        for i in issues:
            state = i.get("state") or {}
            color = _state_color(state)
            pri = i.get("priority", 0)
            pri_color = PRIORITY_COLORS.get(pri, "#95a2b3")
            assignee = (i.get("assignee") or {}).get("name", "\u2014")
            ident = _e(i.get("identifier", "?"))
            href = f"/issues/{quote(i.get('identifier', i.get('id', '')))}"
            rows += f"""<tr class="clickable" onclick="location.href='{href}'">
<td><a href="{href}">{ident}</a></td>
<td>{_e(i.get("title", ""))}</td>
<td><span class="dot" style="background:{color}"></span>{_e(state.get("name", ""))}</td>
<td><span class="badge" style="background:{pri_color}">{PRIORITY_LABELS.get(pri, "None")}</span></td>
<td>{_e(assignee)}</td>
</tr>"""
        body = f"<h1>Issues ({len(issues)})</h1><table><tr><th>ID</th><th>Title</th><th>Status</th><th>Priority</th><th>Assignee</th></tr>{rows}</table>"
        self._send(200, self._page("Issues", body, active="issues"))

    def _serve_issue_detail(self, id_or_identifier: str) -> None:
        issue = self.data.get_issue(id_or_identifier)
        if not issue:
            self._error_page(404, f"Issue '{_e(id_or_identifier)}' not found")
            return

        state = issue.get("state") or {}
        pri = issue.get("priority", 0)
        assignee = (issue.get("assignee") or {}).get("name", "Unassigned")
        project = (issue.get("project") or {}).get("name", "\u2014")
        labels_nodes = (issue.get("labels") or {}).get("nodes", [])
        labels_html = "".join(
            f'<span class="label" style="border-color:{_e(l.get("color", "#e0e0e0"))}">{_e(l.get("name", ""))}</span>'
            for l in labels_nodes
        )

        comments_data = self.data.get_comments(issue["id"])
        comments_nodes = comments_data.get("nodes", [])
        comments_html = ""
        for c in comments_nodes:
            author = (c.get("user") or {}).get("name", "?")
            comments_html += f"""<div class="comment">
<div class="comment-author">{_e(author)}<span class="comment-date">{_e(c.get("createdAt", ""))}</span></div>
<div class="comment-body">{_e(c.get("body", ""))}</div>
</div>"""

        body = f"""
<h1>{_e(issue.get("identifier", ""))} — {_e(issue.get("title", ""))}</h1>
<div class="detail">
  <div class="meta-row">
    <div class="meta-item"><span class="meta-label">Status</span> <span class="dot" style="background:{_state_color(state)}"></span><span class="meta-value">{_e(state.get("name", ""))}</span></div>
    <div class="meta-item"><span class="meta-label">Priority</span> <span class="badge" style="background:{PRIORITY_COLORS.get(pri, "#95a2b3")}">{PRIORITY_LABELS.get(pri, "None")}</span></div>
    <div class="meta-item"><span class="meta-label">Assignee</span> <span class="meta-value">{_e(assignee)}</span></div>
    <div class="meta-item"><span class="meta-label">Project</span> <span class="meta-value">{_e(project)}</span></div>
  </div>
  {f'<div class="labels-row">{labels_html}</div>' if labels_html else ""}
  <div class="description">{_e(issue.get("description", ""))}</div>
</div>
<h2>Comments ({len(comments_nodes)})</h2>
{comments_html or '<div class="meta">No comments</div>'}"""
        self._send(200, self._page(issue.get("identifier", "Issue"), body, active="issues"))

    def _serve_project_list(self) -> None:
        projects = self.data.all_projects()
        rows = ""
        for p in projects:
            lead = (p.get("lead") or {}).get("name", "\u2014")
            state = _e(p.get("state", ""))
            progress = _pct(p.get("progress", 0))
            href = f"/projects/{quote(p.get('id', ''))}"
            rows += f"""<tr class="clickable" onclick="location.href='{href}'">
<td><a href="{href}">{_e(p.get("name", ""))}</a></td>
<td>{state}</td>
<td><div class="progress-bar"><div class="progress-fill" style="width:{progress}%"></div></div>{progress}%</td>
<td>{_e(lead)}</td>
</tr>"""
        body = f"<h1>Projects ({len(projects)})</h1><table><tr><th>Name</th><th>State</th><th>Progress</th><th>Lead</th></tr>{rows}</table>"
        self._send(200, self._page("Projects", body, active="projects"))

    def _serve_project_detail(self, id_or_name: str) -> None:
        project = self.data.get_project(id_or_name)
        if not project:
            self._error_page(404, f"Project '{_e(id_or_name)}' not found")
            return

        lead = (project.get("lead") or {}).get("name", "\u2014")
        project_issues = [i for i in self.data.all_issues() if (i.get("project") or {}).get("id") == project.get("id")]
        issue_rows = ""
        for i in project_issues:
            state = i.get("state") or {}
            ident = _e(i.get("identifier", "?"))
            href = f"/issues/{quote(i.get('identifier', i.get('id', '')))}"
            issue_rows += f"""<tr class="clickable" onclick="location.href='{href}'">
<td><a href="{href}">{ident}</a></td>
<td>{_e(i.get("title", ""))}</td>
<td><span class="dot" style="background:{_state_color(state)}"></span>{_e(state.get("name", ""))}</td>
</tr>"""

        progress = _pct(project.get("progress", 0))
        body = f"""
<h1>{_e(project.get("name", ""))}</h1>
<div class="detail">
  <div class="meta-row">
    <div class="meta-item"><span class="meta-label">State</span> <span class="meta-value">{_e(project.get("state", ""))}</span></div>
    <div class="meta-item"><span class="meta-label">Progress</span> <div class="progress-bar"><div class="progress-fill" style="width:{progress}%"></div></div><span class="meta-value">{progress}%</span></div>
    <div class="meta-item"><span class="meta-label">Lead</span> <span class="meta-value">{_e(lead)}</span></div>
  </div>
  <div class="description">{_e(project.get("description", ""))}</div>
</div>
<h2>Issues ({len(project_issues)})</h2>
{f"<table><tr><th>ID</th><th>Title</th><th>Status</th></tr>{issue_rows}</table>" if issue_rows else '<div class="meta">No issues</div>'}"""
        self._send(200, self._page(project.get("name", "Project"), body, active="projects"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class LinearFrontend:
    """Read-only web frontend for mock Linear data.

    Args:
        data: The ``MockLinearData`` instance to read from.
        host: Bind address (default ``"0.0.0.0"``).
        port: Bind port (default ``0`` = OS-assigned, use fixed port for docker).
    """

    def __init__(
        self,
        data: MockLinearData,
        *,
        host: str = "0.0.0.0",
        port: int = 0,
        detach: bool = False,
    ) -> None:
        self._data = data
        self._host = host
        self._port = port
        self._detach = detach and hasattr(os, "fork")
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._child_pid: int | None = None

    def start(self) -> None:
        """Start the HTTP server. Idempotent.

        When ``detach=True``, forks a child process so the server survives
        the parent exiting (needed for short-lived ``uv run dev`` setup
        scripts).  Otherwise uses a daemon thread (default).
        """
        if self._httpd is not None or self._child_pid is not None:
            return

        handler_cls = type("_Handler", (_LinearHandler,), {"data": self._data})
        self._httpd = HTTPServer((self._host, self._port), handler_cls)
        self._port = self._httpd.server_address[1]

        if self._detach:
            pid = os.fork()
            if pid == 0:
                os.setsid()
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                self._httpd.serve_forever()
                os._exit(0)
            self._child_pid = pid
            self._httpd.server_close()
            self._httpd = None
        else:
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                daemon=True,
                name="linear-frontend",
            )
            self._thread.start()

        logger.info("LinearFrontend started on %s:%d", self._host, self._port)

    def stop(self) -> None:
        """Shut down the HTTP server. Idempotent."""
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
                os.waitpid(self._child_pid, 0)
            except OSError:
                pass
            self._child_pid = None
            logger.info("LinearFrontend stopped")
            return
        if self._httpd is None:
            return
        self._httpd.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd.server_close()
        self._httpd = None
        self._thread = None
        logger.info("LinearFrontend stopped")

    @property
    def url(self) -> str:
        """Base URL, e.g. ``http://0.0.0.0:8080``."""
        return f"http://{self._host}:{self._port}"

    def __enter__(self) -> LinearFrontend:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
