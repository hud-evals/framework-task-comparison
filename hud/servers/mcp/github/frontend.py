"""GitHub frontend — browse mock GitHub state in a browser.

Runs a stdlib ``http.server`` in a daemon thread, reading directly from
``MockGitHubClient`` internals.  No async needed.

Templates and static assets live in ``ui/``.  Templates use
``{{placeholder}}`` syntax, substituted via single-pass ``re.sub``
(safe against injection from content values).

Usage::

    from servers.mcp.github import MockGitHubClient, GitHubFrontend

    client = MockGitHubClient(bare_repo_path="/srv/git/project.git", ...)
    client.load("/data")

    with GitHubFrontend(client, port=8081) as fe:
        print(fe.url)
        ...
"""

from __future__ import annotations

import html
import logging
import mimetypes
import os
import re
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .mock_client import GitHubAPIError, MockGitHubClient

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).parent / "ui"

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

_FILE_LIMIT = 100 * 1024  # 100 KB
_DIFF_LIMIT = 200 * 1024  # 200 KB
_COMMIT_LIMIT = 50


def _render(name: str, **kwargs: str) -> str:
    """Load a template file and substitute ``{{placeholders}}`` in one pass.

    Uses ``re.sub`` so that values injected for one placeholder are never
    re-scanned — arbitrary content (e.g. source code with ``{{…}}``) is safe.
    """
    template = (_UI_DIR / name).read_text()

    def replacer(m: re.Match[str]) -> str:
        key = m.group(1)
        assert isinstance(key, str)
        return kwargs.get(key, m.group(0))

    return _PLACEHOLDER_RE.sub(replacer, template)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class _GitHubHandler(BaseHTTPRequestHandler):
    """HTTP request handler — routes dispatched by path segments."""

    clients: dict[tuple[str, str], MockGitHubClient]
    worktree_path: str | None = None

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    # -- worktree helper ----------------------------------------------------

    _WORKTREE_OUTPUT_LIMIT = 1024 * 1024  # 1 MB per call

    def _worktree_git(self, *args: str) -> subprocess.CompletedProcess | None:
        """Run git in the worktree dir.  Returns None on any error."""
        if not self.worktree_path:
            return None
        try:
            proc = subprocess.Popen(
                ["git", *args],
                cwd=self.worktree_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            cap = self._WORKTREE_OUTPUT_LIMIT
            stdout = proc.stdout.read(cap)  # type: ignore[union-attr]
            stderr = proc.stderr.read(cap)  # type: ignore[union-attr]
            proc.kill()
            proc.wait(timeout=5)
            return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        except Exception:
            return None

    # -- data helpers -------------------------------------------------------

    def _get_issues(self) -> list[dict]:
        return self.client._issues + self.client._created_issues

    def _get_prs(self) -> list[dict]:
        return list(self.client._pull_requests)

    def _get_comments(self, number: int) -> list[dict]:
        return self.client._comments.get(number, [])

    def _resolve_ref(self, ref: str) -> str | None:
        try:
            r = self.client._git("rev-parse", "--verify", ref, check=False)
            return r.stdout.strip() if r.returncode == 0 else None
        except GitHubAPIError:
            return None

    def _get_branches(self) -> list[tuple[str, str]]:
        try:
            r = self.client._git(
                "for-each-ref",
                "--format=%(refname:short) %(objectname:short)",
                "refs/heads/",
                check=False,
            )
        except GitHubAPIError:
            return []
        if r.returncode != 0:
            return []
        branches = []
        for line in r.stdout.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                branches.append((parts[0], parts[1]))
        return branches

    def _get_branch_details(self) -> list[dict]:
        try:
            r = self.client._git(
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)%09%(objectname:short)%09%(authorname)%09%(committerdate:relative)%09%(subject)",
                "refs/heads/",
                check=False,
            )
        except GitHubAPIError:
            return []
        if r.returncode != 0:
            return []
        branches = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t", 4)
            if len(parts) == 5:
                branches.append(
                    {
                        "name": parts[0],
                        "sha": parts[1],
                        "author": parts[2],
                        "date": parts[3],
                        "message": parts[4],
                    }
                )
        return branches

    def _get_ahead_behind(self, branch: str, base: str) -> tuple[int, int]:
        try:
            r = self.client._git("rev-list", "--left-right", "--count", f"{base}...{branch}", check=False)
        except GitHubAPIError:
            return 0, 0
        if r.returncode != 0:
            return 0, 0
        parts = r.stdout.strip().split()
        if len(parts) == 2:
            return int(parts[1]), int(parts[0])  # ahead, behind
        return 0, 0

    def _get_commit_count(self, ref: str) -> int:
        try:
            r = self.client._git("rev-list", "--count", ref, check=False)
        except GitHubAPIError:
            return 0
        return int(r.stdout.strip()) if r.returncode == 0 else 0

    def _get_last_commit(self, ref: str) -> dict | None:
        try:
            r = self.client._git(
                "log",
                "-1",
                "--format=%H%n%an%n%ar%n%s",
                ref,
                check=False,
            )
        except GitHubAPIError:
            return None
        if r.returncode != 0:
            return None
        lines = r.stdout.strip().split("\n")
        if len(lines) >= 4:
            return {"sha": lines[0], "author": lines[1], "date": lines[2], "message": lines[3]}
        return None

    def _find_pr_for_branch(self, branch: str) -> dict | None:
        for pr in self.client._pull_requests:
            if pr.get("head", {}).get("ref") == branch and pr.get("state") == "open":
                return pr
        return None

    def _get_tree(self, ref: str, path: str) -> list[tuple[str, str]] | None:
        if self._resolve_ref(ref) is None:
            return None
        target = f"{ref}:{path}" if path else ref
        try:
            r = self.client._git("ls-tree", "--name-only", target, check=False)
        except GitHubAPIError:
            return None
        if r.returncode != 0:
            return None
        entries = []
        for name in r.stdout.strip().splitlines():
            if not name:
                continue
            full = f"{path}/{name}" if path else name
            try:
                tr = self.client._git("cat-file", "-t", f"{ref}:{full}", check=False)
            except GitHubAPIError:
                tr = None
            etype = "tree" if (tr and tr.stdout.strip() == "tree") else "blob"
            entries.append((etype, name))
        entries.sort(key=lambda e: (0 if e[0] == "tree" else 1, e[1].lower()))
        return entries

    def _get_blob(self, ref: str, path: str) -> str | None:
        if self._resolve_ref(ref) is None:
            return None
        try:
            r = self.client._git("show", f"{ref}:{path}", check=False)
        except GitHubAPIError:
            return None
        return r.stdout if r.returncode == 0 else None

    def _get_commits(self, ref: str, n: int = _COMMIT_LIMIT) -> list[dict] | None:
        if self._resolve_ref(ref) is None:
            return None
        try:
            r = self.client._git("log", "--format=%H%n%an%n%aI%n%s", f"-{n}", ref, check=False)
        except GitHubAPIError:
            return None
        if r.returncode != 0:
            return None
        commits = []
        lines = r.stdout.strip().split("\n")
        i = 0
        while i + 3 < len(lines):
            commits.append(
                {
                    "sha": lines[i],
                    "author": lines[i + 1],
                    "date": lines[i + 2],
                    "message": lines[i + 3],
                }
            )
            i += 4
        return commits

    def _get_diff(self, base: str, head: str) -> str | None:
        try:
            r = self.client._git("diff", f"{base}..{head}", check=False)
        except GitHubAPIError:
            return None
        return r.stdout if r.returncode == 0 else None

    def _get_commit_detail(self, sha: str) -> dict | None:
        """Return full commit info + diff patch for a single commit."""
        try:
            r = self.client._git(
                "show",
                "--format=%H%n%an%n%ae%n%aI%n%ar%n%s%n%b",
                "--stat",
                sha,
                check=False,
            )
        except GitHubAPIError:
            return None
        if r.returncode != 0:
            return None
        lines = r.stdout.split("\n")
        if len(lines) < 6:
            return None
        try:
            dr = self.client._git("diff-tree", "-p", "--root", sha, check=False)
        except GitHubAPIError:
            dr = None
        diff = dr.stdout if dr and dr.returncode == 0 else ""
        return {
            "sha": lines[0],
            "author": lines[1],
            "email": lines[2],
            "date_iso": lines[3],
            "date_rel": lines[4],
            "subject": lines[5],
            "body": "\n".join(lines[6:]).split("\n\n")[0].strip(),
            "stat": r.stdout,
            "diff": diff,
        }

    def _has_repo(self) -> bool:
        return self.client.bare_repo_path is not None

    # -- response helpers ---------------------------------------------------

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _page(self, title: str, content: str, active: str = "") -> str:
        c = self.client
        base = f"/{c.repo_owner}/{c.repo_name}"
        e = html.escape

        def nav(label: str, href: str, key: str) -> str:
            cls = ' class="active"' if active == key else ""
            return f'<a href="{e(href)}"{cls}>{e(label)}</a>'

        nav_items = [
            nav("Code", base, "code"),
            nav("Issues", base + "/issues", "issues"),
            nav("Pull Requests", base + "/pulls", "pulls"),
        ]
        if self.worktree_path:
            nav_items.append(nav("Worktree", base + "/worktree", "worktree"))
        nav_html = "\n  ".join(nav_items)

        header_url = "/" if len(self.clients) > 1 else base
        return _render(
            "layout.html",
            title=e(title),
            base_url=e(header_url),
            repo_fullname=e(f"{c.repo_owner}/{c.repo_name}"),
            nav=nav_html,
            content=content,
        )

    def _error_page(self, status: int, message: str) -> None:
        body = self._page(str(status), f'<p class="meta">{html.escape(message)}</p>')
        self._send(status, body)

    def _handle_repo_list(self) -> None:
        e = html.escape
        rows = []
        for owner, repo in sorted(self.clients):
            url = f"/{owner}/{repo}"
            rows.append(f'<tr><td><a href="{url}">{e(owner)}/{e(repo)}</a></td></tr>')
        body = _render(
            "layout.html",
            title="Repositories",
            base_url="/",
            repo_fullname="GitHub",
            nav="",
            content=f"<table>{''.join(rows)}</table>",
        )
        self._send(200, body)

    def _serve_static(self, path: str) -> None:
        """Serve a file from ``ui/static/``."""
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

    # -- shared UI components -----------------------------------------------

    def _render_code_header(self, ref: str, path: str) -> str:
        e = html.escape
        c = self.client
        base = f"/{c.repo_owner}/{c.repo_name}"

        branches = self._get_branches()
        ref_options = "".join(
            f'<option value="{e(b[0])}"{"selected" if b[0] == ref else ""}>{e(b[0])}</option>' for b in branches
        )

        path_suffix = f"/{quote(path, safe='/')}" if path else ""
        form_action = e(f"{base}/tree{path_suffix}")
        branches_url = e(f"{base}/branches")

        header = (
            f'<div class="code-header">'
            f'<div class="ref-selector">'
            f'<form method="get" action="{form_action}" style="display:inline">'
            f'<select name="ref" onchange="this.form.submit()">{ref_options}</select>'
            f"</form></div>"
            f'<a href="{branches_url}" class="branch-count">{len(branches)} branches</a>'
            f"</div>"
        )

        last = self._get_last_commit(ref)
        if last:
            total = self._get_commit_count(ref)
            commits_url = e(f"{base}/commits?ref={quote(ref)}")
            commit_url = f"{base}/commit/{last['sha']}"
            sha_short = e(last["sha"][:7])
            header += (
                f'<div class="commit-bar">'
                f'<span class="author">{e(last["author"])}</span>'
                f'<a href="{commit_url}" class="msg">{e(last["message"])}</a>'
                f'<a href="{commit_url}" class="sha">{sha_short}</a>'
                f'<span class="time">{e(last["date"])}</span>'
                f'<a href="{commits_url}" class="total">{total} commits</a>'
                f"</div>"
            )

        return header

    def _render_diff(self, diff: str, limit: int = _DIFF_LIMIT) -> str:
        """Render a unified diff with line-level coloring."""
        e = html.escape
        truncated = len(diff) > limit
        lines = diff[:limit].split("\n")
        parts = ['<div class="diff">']
        for line in lines:
            if (
                line.startswith("diff ")
                or line.startswith("index ")
                or line.startswith("---")
                or line.startswith("+++")
            ):
                cls = "diff-meta"
            elif line.startswith("@@"):
                cls = "diff-hunk"
            elif line.startswith("+"):
                cls = "diff-add"
            elif line.startswith("-"):
                cls = "diff-del"
            else:
                cls = "diff-ctx"
            parts.append(f'<div class="{cls}">{e(line)}</div>')
        parts.append("</div>")
        if truncated:
            parts.append('<p class="truncated">Diff truncated at 200 KB</p>')
        return "\n".join(parts)

    def _render_comments(self, comments: list[dict]) -> str:
        if not comments:
            return ""
        e = html.escape
        parts = ["<h2>Comments</h2>"]
        for cm in comments:
            user = cm.get("user", {}).get("login", "unknown")
            date = cm.get("created_at", "")
            body = e(cm.get("body", ""))
            parts.append(
                f'<div class="comment">'
                f'<p class="meta"><strong>{e(user)}</strong> &middot; {e(date)}</p>'
                f"<pre><code>{body}</code></pre></div>"
            )
        return "\n".join(parts)

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        self.client = next(iter(self.clients.values()))

        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return

        segments = [unquote(s) for s in path.split("/") if s]

        if path == "/":
            if len(self.clients) == 1:
                owner, repo = next(iter(self.clients))
                self._redirect(f"/{owner}/{repo}")
            else:
                self._handle_repo_list()
            return

        if len(segments) < 2:
            self._error_page(404, "Not found")
            return

        key = (segments[0], segments[1])
        if key not in self.clients:
            self._error_page(404, f"Repository {segments[0]}/{segments[1]} not found")
            return

        self.client = self.clients[key]
        rest = segments[2:]
        ref = query.get("ref", [self.client.default_branch])[0]

        if not rest:
            self._handle_tree(ref, "")
        elif rest[0] == "issues":
            if len(rest) == 1:
                self._handle_issue_list()
            elif len(rest) == 2:
                self._handle_issue_detail(rest[1])
            else:
                self._error_page(404, "Not found")
        elif rest[0] == "pulls":
            if len(rest) == 1:
                self._handle_pr_list()
            elif len(rest) == 2:
                self._handle_pr_detail(rest[1])
            else:
                self._error_page(404, "Not found")
        elif rest[0] == "tree":
            self._handle_tree(ref, "/".join(rest[1:]))
        elif rest[0] == "blob":
            self._handle_blob(ref, "/".join(rest[1:]))
        elif rest[0] == "commit" and len(rest) == 2:
            self._handle_commit_detail(rest[1])
        elif rest[0] == "commits":
            self._handle_commits(ref)
        elif rest[0] == "branches":
            self._handle_branches()
        elif rest[0] == "worktree":
            wt_ref = query.get("ref", [None])[0]
            self._handle_worktree(wt_ref)
        else:
            self._error_page(404, "Not found")

    # -- route handlers -----------------------------------------------------

    def _handle_tree(self, ref: str, path: str) -> None:
        if not self._has_repo():
            self._error_page(404, "Repository not configured")
            return

        entries = self._get_tree(ref, path)
        if entries is None:
            self._error_page(404, f"Path not found: {path or '/'} at ref {ref}")
            return

        e = html.escape
        c = self.client
        base = f"/{c.repo_owner}/{c.repo_name}"

        breadcrumb = ""
        if path:
            crumbs = [f'<a href="{base}/tree?ref={quote(ref)}">{e(c.repo_name)}</a>']
            acc: list[str] = []
            for part in path.split("/"):
                acc.append(part)
                p = "/".join(acc)
                crumbs.append(f'<a href="{base}/tree/{quote(p, safe="/")}?ref={quote(ref)}">{e(part)}</a>')
            breadcrumb = f'<div class="breadcrumb">{" / ".join(crumbs)}</div>'

        rows = []
        for etype, name in entries:
            full = f"{path}/{name}" if path else name
            if etype == "tree":
                href = f"{base}/tree/{quote(full, safe='/')}?ref={quote(ref)}"
                icon = "\U0001f4c1"
            else:
                href = f"{base}/blob/{quote(full, safe='/')}?ref={quote(ref)}"
                icon = "\U0001f4c4"
            rows.append(f'<tr><td><span class="icon">{icon}</span><a href="{href}">{e(name)}</a></td></tr>')

        if not rows:
            file_table = '<p class="empty">Empty directory</p>'
        else:
            file_table = f"<table>{''.join(rows)}</table>"

        readme = ""
        if not path:
            rc = self._get_blob(ref, "README.md")
            if rc is None:
                rc = self._get_blob(ref, "readme.md")
            if rc is not None:
                readme = f"<h2>README</h2><pre><code>{e(rc[:_FILE_LIMIT])}</code></pre>"
                if len(rc) > _FILE_LIMIT:
                    readme += '<p class="truncated">Truncated at 100 KB</p>'

        content = f"{self._render_code_header(ref, path)}\n{breadcrumb}\n{file_table}\n{readme}"
        title = f"{c.repo_owner}/{c.repo_name}" if not path else path
        self._send(200, self._page(title, content, "code"))

    def _handle_blob(self, ref: str, path: str) -> None:
        if not self._has_repo():
            self._error_page(404, "Repository not configured")
            return
        if not path:
            self._error_page(400, "No file path specified")
            return

        file_content = self._get_blob(ref, path)
        if file_content is None:
            self._error_page(404, f"File not found: {path} at ref {ref}")
            return

        e = html.escape
        c = self.client
        base = f"/{c.repo_owner}/{c.repo_name}"

        crumbs = [f'<a href="{base}/tree?ref={quote(ref)}">{e(c.repo_name)}</a>']
        acc: list[str] = []
        path_parts = path.split("/")
        for i, part in enumerate(path_parts):
            acc.append(part)
            p = "/".join(acc)
            if i < len(path_parts) - 1:
                crumbs.append(f'<a href="{base}/tree/{quote(p, safe="/")}?ref={quote(ref)}">{e(part)}</a>')
            else:
                crumbs.append(e(part))

        truncated = '<p class="truncated">Truncated at 100 KB</p>' if len(file_content) > _FILE_LIMIT else ""
        content = (
            f'<div class="breadcrumb">{" / ".join(crumbs)}</div>'
            f'<p class="meta">ref: <code>{e(ref)}</code> &middot; {len(file_content)} bytes</p>'
            f"<pre><code>{e(file_content[:_FILE_LIMIT])}</code></pre>"
            f"{truncated}"
        )
        self._send(200, self._page(path_parts[-1], content, "code"))

    def _handle_branches(self) -> None:
        if not self._has_repo():
            self._error_page(404, "Repository not configured")
            return

        branches = self._get_branch_details()
        e = html.escape
        c = self.client
        base = f"/{c.repo_owner}/{c.repo_name}"
        default = c.default_branch

        if not branches:
            table = '<p class="empty">No branches</p>'
        else:
            rows = []
            for b in branches:
                name = b["name"]
                tree_link = f'<a href="{base}/tree?ref={quote(name)}">{e(name)}</a>'
                default_tag = ' <span class="badge badge-default">default</span>' if name == default else ""

                if name == default:
                    ab_html = '<span class="meta">\u2014</span>'
                else:
                    ahead, behind = self._get_ahead_behind(name, default)
                    ab_parts = []
                    if behind:
                        ab_parts.append(f'<span class="behind">{behind} behind</span>')
                    if ahead:
                        ab_parts.append(f'<span class="ahead">{ahead} ahead</span>')
                    ab_html = " \u00b7 ".join(ab_parts) if ab_parts else '<span class="meta">even</span>'

                pr = self._find_pr_for_branch(name)
                if pr:
                    pr_num = pr.get("number", "?")
                    pr_title = e(str(pr.get("title", "")))[:40]
                    draft = pr.get("draft", False)
                    pr_cls = "badge-draft" if draft else "badge-open"
                    pr_label = "draft" if draft else "open"
                    pr_html = (
                        f'<a href="{base}/pulls/{pr_num}">#{pr_num} {pr_title}</a> '
                        f'<span class="badge {pr_cls}">{pr_label}</span>'
                    )
                else:
                    pr_html = '<span class="meta">\u2014</span>'

                rows.append(
                    f"<tr>"
                    f"<td>{tree_link}{default_tag}</td>"
                    f'<td class="meta">{e(b["author"])}</td>'
                    f'<td class="meta">{e(b["date"])}</td>'
                    f"<td>{ab_html}</td>"
                    f"<td>{pr_html}</td>"
                    f"</tr>"
                )
            table = (
                f"<table><tr><th>Branch</th><th>Author</th><th>Updated</th>"
                f"<th>Status</th><th>Pull Request</th></tr>{''.join(rows)}</table>"
            )

        self._send(200, self._page("Branches", table, "code"))

    def _handle_issue_list(self) -> None:
        issues = self._get_issues()
        e = html.escape
        c = self.client
        base = f"/{c.repo_owner}/{c.repo_name}"

        if not issues:
            table = '<p class="empty">No issues</p>'
        else:
            rows = []
            for iss in issues:
                num = iss.get("number", "?")
                title = e(str(iss.get("title", "")))
                state = iss.get("state", "open")
                badge_cls = "badge-open" if state == "open" else "badge-closed"
                labels_html = ""
                for lbl in iss.get("labels", []):
                    labels_html += f' <span class="label">{e(str(lbl.get("name", "")))}</span>'
                rows.append(
                    f'<tr><td><a href="{base}/issues/{num}">#{num}</a></td>'
                    f'<td><a href="{base}/issues/{num}">{title}</a>{labels_html}</td>'
                    f'<td><span class="badge {badge_cls}">{e(state)}</span></td></tr>'
                )
            table = f"<table><tr><th>#</th><th>Title</th><th>State</th></tr>{''.join(rows)}</table>"

        self._send(200, self._page("Issues", table, "issues"))

    def _handle_issue_detail(self, num_str: str) -> None:
        try:
            num = int(num_str)
        except ValueError:
            self._error_page(400, f"Invalid issue number: {num_str}")
            return

        e = html.escape
        issue = None
        for iss in self._get_issues():
            if iss.get("number") == num:
                issue = iss
                break

        if issue is None:
            self._error_page(404, f"Issue #{num} not found")
            return

        state = issue.get("state", "open")
        badge_cls = "badge-open" if state == "open" else "badge-closed"
        labels_html = ""
        for lbl in issue.get("labels", []):
            labels_html += f' <span class="label">{e(str(lbl.get("name", "")))}</span>'

        content = (
            f'<p><span class="badge {badge_cls}">{e(state)}</span>{labels_html}</p>'
            f"<pre><code>{e(issue.get('body') or '')}</code></pre>"
            f"{self._render_comments(self._get_comments(num))}"
        )
        title = str(issue.get("title", ""))
        self._send(200, self._page(f"Issue #{num}: {title}", content, "issues"))

    def _handle_pr_list(self) -> None:
        prs = self._get_prs()
        e = html.escape
        c = self.client
        base = f"/{c.repo_owner}/{c.repo_name}"

        if not prs:
            table = '<p class="empty">No pull requests</p>'
        else:
            rows = []
            for pr in prs:
                num = pr.get("number", "?")
                title = e(str(pr.get("title", "")))
                state = pr.get("state", "open")
                merged = pr.get("merged", False)
                draft = pr.get("draft", False)
                if merged:
                    badge_cls, label = "badge-merged", "merged"
                elif draft:
                    badge_cls, label = "badge-draft", "draft"
                elif state == "open":
                    badge_cls, label = "badge-open", "open"
                else:
                    badge_cls, label = "badge-closed", "closed"
                head = e(str(pr.get("head", {}).get("ref", "?")))
                base_ref = e(str(pr.get("base", {}).get("ref", "?")))
                rows.append(
                    f'<tr><td><a href="{base}/pulls/{num}">#{num}</a></td>'
                    f'<td><a href="{base}/pulls/{num}">{title}</a></td>'
                    f"<td>{head} \u2192 {base_ref}</td>"
                    f'<td><span class="badge {badge_cls}">{e(label)}</span></td></tr>'
                )
            table = f"<table><tr><th>#</th><th>Title</th><th>Branches</th><th>State</th></tr>{''.join(rows)}</table>"

        self._send(200, self._page("Pull Requests", table, "pulls"))

    def _handle_pr_detail(self, num_str: str) -> None:
        try:
            num = int(num_str)
        except ValueError:
            self._error_page(400, f"Invalid PR number: {num_str}")
            return

        e = html.escape
        pr = None
        for p in self._get_prs():
            if p.get("number") == num:
                pr = p
                break

        if pr is None:
            self._error_page(404, f"Pull request #{num} not found")
            return

        state = pr.get("state", "open")
        merged = pr.get("merged", False)
        draft = pr.get("draft", False)
        if merged:
            badge_cls, label = "badge-merged", "merged"
        elif draft:
            badge_cls, label = "badge-draft", "draft"
        elif state == "open":
            badge_cls, label = "badge-open", "open"
        else:
            badge_cls, label = "badge-closed", "closed"

        head_ref = str(pr.get("head", {}).get("ref", "?"))
        base_ref = str(pr.get("base", {}).get("ref", "?"))

        diff_section = ""
        if self._has_repo():
            diff = self._get_diff(base_ref, head_ref)
            if diff is not None:
                diff_section = f"<h2>Diff</h2>{self._render_diff(diff)}"

        content = (
            f'<p><span class="badge {badge_cls}">{e(label)}</span></p>'
            f'<p class="meta">{e(head_ref)} \u2192 {e(base_ref)}</p>'
            f"<pre><code>{e(pr.get('body') or '')}</code></pre>"
            f"{diff_section}"
            f"{self._render_comments(self._get_comments(num))}"
        )
        title = str(pr.get("title", ""))
        self._send(200, self._page(f"PR #{num}: {title}", content, "pulls"))

    def _handle_commits(self, ref: str) -> None:
        if not self._has_repo():
            self._error_page(404, "Repository not configured")
            return

        commits = self._get_commits(ref)
        if commits is None:
            self._error_page(404, f"Ref not found: {ref}")
            return

        e = html.escape
        base = f"/{self.client.repo_owner}/{self.client.repo_name}"
        branches = self._get_branches()
        ref_options = "".join(
            f'<option value="{e(b[0])}"{"selected" if b[0] == ref else ""}>{e(b[0])}</option>' for b in branches
        )
        ref_selector = (
            f'<div class="ref-selector" style="margin-bottom:16px">'
            f'<form method="get" style="display:inline">'
            f'<select name="ref" onchange="this.form.submit()">{ref_options}</select>'
            f"</form></div>"
        )

        if not commits:
            commit_table = '<p class="empty">No commits</p>'
        else:
            rows = []
            for cm in commits:
                sha_short = cm["sha"][:8]
                commit_url = f"{base}/commit/{cm['sha']}"
                rows.append(
                    f'<tr><td><a href="{commit_url}"><code>{e(sha_short)}</code></a></td>'
                    f'<td><a href="{commit_url}">{e(cm["message"])}</a></td>'
                    f'<td class="meta">{e(cm["author"])}</td>'
                    f'<td class="meta">{e(cm["date"])}</td></tr>'
                )
            commit_table = (
                f"<table><tr><th>SHA</th><th>Message</th><th>Author</th><th>Date</th></tr>{''.join(rows)}</table>"
            )

        self._send(200, self._page(f"Commits ({ref})", f"{ref_selector}\n{commit_table}", "code"))

    def _handle_commit_detail(self, sha: str) -> None:
        if not self._has_repo():
            self._error_page(404, "Repository not configured")
            return

        detail = self._get_commit_detail(sha)
        if detail is None:
            self._error_page(404, f"Commit not found: {sha}")
            return

        e = html.escape
        full_sha = detail["sha"]
        diff = detail["diff"]

        content = (
            f'<p class="meta">'
            f"{e(detail['author'])} &lt;{e(detail['email'])}&gt; &middot; "
            f"{e(detail['date_rel'])} &middot; <code>{e(full_sha)}</code></p>"
        )
        if detail["body"]:
            content += f"<pre><code>{e(detail['body'])}</code></pre>"
        content += f"<h2>Changes</h2>{self._render_diff(diff)}"

        self._send(200, self._page(detail["subject"], content, "code"))

    def _handle_worktree(self, ref: str | None = None) -> None:
        e = html.escape
        c = self.client
        base = f"/{c.repo_owner}/{c.repo_name}"

        if not self.worktree_path:
            self._send(200, self._page("Worktree", '<p class="meta">Worktree not configured</p>', "worktree"))
            return

        r = self._worktree_git("rev-parse", "--git-dir")
        if r is None or r.returncode != 0:
            msg = f'<p class="meta">Not a git repository: {e(self.worktree_path)}</p>'
            self._send(200, self._page("Worktree", msg, "worktree"))
            return

        sections: list[str] = []

        r_branch = self._worktree_git("branch", "--show-current")
        current_branch = r_branch.stdout.strip() if r_branch and r_branch.returncode == 0 else ""
        selected = ref or current_branch or "HEAD"

        r_log = self._worktree_git("log", "-1", "--format=%H%n%s", selected)
        if r_log and r_log.returncode == 0:
            log_lines = r_log.stdout.strip().split("\n", 1)
            sel_sha = log_lines[0]
            sel_msg = log_lines[1] if len(log_lines) > 1 else ""
        else:
            sel_sha, sel_msg = "", ""

        hero_stat: list[str] = []
        if self._has_repo() and selected != "HEAD":
            bare_sha = self._resolve_ref(f"refs/heads/{selected}")
            if bare_sha is None:
                hero_stat.append('<span class="wt-pill wt-pill-blue">not pushed</span>')
            elif bare_sha == sel_sha:
                hero_stat.append('<span class="wt-pill wt-pill-clean">up to date</span>')
            else:
                r_a = self._worktree_git("rev-list", "--count", f"{bare_sha}..{selected}")
                r_b = self._worktree_git("rev-list", "--count", f"{selected}..{bare_sha}")
                ahead = int(r_a.stdout.strip()) if r_a and r_a.returncode == 0 else 0
                behind = int(r_b.stdout.strip()) if r_b and r_b.returncode == 0 else 0
                if ahead:
                    hero_stat.append(f'<span class="wt-pill wt-pill-green">\u2191 {ahead} ahead</span>')
                if behind:
                    hero_stat.append(f'<span class="wt-pill wt-pill-red">\u2193 {behind} behind</span>')
                if not ahead and not behind:
                    hero_stat.append('<span class="wt-pill wt-pill-clean">up to date</span>')

        status_error = ""
        if selected == current_branch or ref is None:
            r_status = self._worktree_git("status", "--porcelain=v1", "-uall")
            if r_status is None or r_status.returncode != 0:
                status_lines = []
                status_error = r_status.stderr.strip() if r_status else "git status failed"
                hero_stat.append('<span class="wt-pill wt-pill-red">status error</span>')
            else:
                status_lines = [l for l in r_status.stdout.splitlines() if l]
                if status_lines:
                    hero_stat.append(f'<span class="wt-pill wt-pill-red">{len(status_lines)} changed</span>')
                else:
                    hero_stat.append('<span class="wt-pill wt-pill-clean">clean</span>')
        else:
            r_status = None
            status_lines = []

        current_tag = ' <span class="badge badge-open">checked out</span>' if selected == current_branch else ""
        sections.append(
            f'<div class="wt-hero">'
            f'<span class="wt-branch">{e(selected)}</span>{current_tag}'
            f'<span class="wt-sha">{e(sel_sha[:8])}</span>'
            f'<span class="wt-msg">{e(sel_msg)}</span>'
            f'<div class="wt-stat">{"".join(hero_stat)}</div>'
            f"</div>"
        )

        r_wt = self._worktree_git("worktree", "list", "--porcelain")
        worktrees: list[dict[str, str]] = []
        if r_wt and r_wt.returncode == 0 and r_wt.stdout.strip():
            wt: dict[str, str] = {}
            for line in r_wt.stdout.splitlines():
                if not line:
                    if wt:
                        worktrees.append(wt)
                    wt = {}
                elif line.startswith("worktree "):
                    wt["path"] = line[9:]
                elif line.startswith("HEAD "):
                    wt["sha"] = line[5:]
                elif line.startswith("branch "):
                    wt["branch"] = line[7:].removeprefix("refs/heads/")
                elif line == "detached":
                    wt["branch"] = "(detached)"
                elif line == "bare":
                    wt["branch"] = "(bare)"
            if wt:
                worktrees.append(wt)

        if len(worktrees) > 1:
            wt_url = f"{base}/worktree"
            rows = []
            for w in worktrees:
                wb = w.get("branch", "?")
                ws = w.get("sha", "?")[:8]
                wp = w.get("path", "?")
                link = (
                    f'<a href="{wt_url}?ref={quote(wb)}">{e(wb)}</a>' if wb not in ("(detached)", "(bare)") else e(wb)
                )
                rows.append(
                    f'<tr><td>{link}</td><td class="meta"><code>{e(ws)}</code></td><td class="meta">{e(wp)}</td></tr>'
                )
            sections.append(
                f'<div class="wt-panel">'
                f'<div class="wt-panel-head">Worktrees <span class="wt-count">{len(worktrees)}</span></div>'
                f"<table><tr><th>Branch</th><th>HEAD</th><th>Path</th></tr>"
                f"{''.join(rows)}</table></div>"
            )

        r_refs = self._worktree_git(
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)%09%(objectname:short)%09%(objectname)%09%(subject)",
            "refs/heads/",
        )
        if r_refs and r_refs.returncode == 0 and r_refs.stdout.strip():
            wt_url = f"{base}/worktree"
            branch_count = 0
            rows = []
            for line in r_refs.stdout.strip().splitlines():
                parts = line.split("\t", 3)
                if len(parts) < 4:
                    continue
                bname, bsha_short, bsha_full, bmsg = parts
                branch_count += 1
                is_current = bname == current_branch
                is_selected = bname == selected

                ab_html = '<span class="meta">\u2014</span>'
                if self._has_repo():
                    b_bare_sha = self._resolve_ref(f"refs/heads/{bname}")
                    if b_bare_sha is None:
                        ab_html = '<span class="wt-pill wt-pill-blue">not pushed</span>'
                    elif b_bare_sha == bsha_full:
                        ab_html = '<span class="wt-pill wt-pill-gray">up to date</span>'
                    else:
                        r_a = self._worktree_git("rev-list", "--count", f"{b_bare_sha}..{bsha_full}")
                        r_b = self._worktree_git("rev-list", "--count", f"{bsha_full}..{b_bare_sha}")
                        ahead = int(r_a.stdout.strip()) if r_a and r_a.returncode == 0 else 0
                        behind = int(r_b.stdout.strip()) if r_b and r_b.returncode == 0 else 0
                        pills = []
                        if ahead:
                            pills.append(f'<span class="wt-pill wt-pill-green">\u2191{ahead}</span>')
                        if behind:
                            pills.append(f'<span class="wt-pill wt-pill-red">\u2193{behind}</span>')
                        ab_html = " ".join(pills) if pills else '<span class="wt-pill wt-pill-gray">even</span>'

                tr_cls = []
                if is_selected:
                    tr_cls.append("wt-selected")
                if is_current:
                    tr_cls.append("wt-current")
                tr_attr = f' class="{" ".join(tr_cls)}"' if tr_cls else ""
                cur_badge = ' <span class="badge badge-default">\u2713</span>' if is_current else ""
                link = f'<a href="{wt_url}?ref={quote(bname)}">{e(bname)}</a>'
                rows.append(
                    f"<tr{tr_attr}><td>{link}{cur_badge}</td>"
                    f'<td class="meta"><code>{e(bsha_short)}</code></td>'
                    f"<td>{ab_html}</td>"
                    f'<td class="meta">{e(bmsg)}</td></tr>'
                )
            sections.append(
                f'<div class="wt-panel">'
                f'<div class="wt-panel-head">Branches <span class="wt-count">{branch_count}</span></div>'
                f"<table><tr><th>Branch</th><th>HEAD</th><th>vs bare</th><th>Message</th></tr>"
                f"{''.join(rows)}</table></div>"
            )

        if self._has_repo() and selected != "HEAD":
            bare_sha = self._resolve_ref(f"refs/heads/{selected}")
            if bare_sha:
                r_clog = self._worktree_git(
                    "log",
                    "--format=%H%n%an%n%ar%n%s",
                    f"{bare_sha}..{selected}",
                )
                if r_clog and r_clog.returncode == 0 and r_clog.stdout.strip():
                    lines = r_clog.stdout.strip().split("\n")
                    commits = []
                    i = 0
                    while i + 3 < len(lines):
                        commits.append(
                            {
                                "sha": lines[i],
                                "author": lines[i + 1],
                                "date": lines[i + 2],
                                "message": lines[i + 3],
                            }
                        )
                        i += 4
                    if commits:
                        crows = []
                        for cm in commits:
                            crows.append(
                                f"<tr><td><code>{e(cm['sha'][:8])}</code></td>"
                                f"<td>{e(cm['message'])}</td>"
                                f'<td class="meta">{e(cm["author"])}</td>'
                                f'<td class="meta">{e(cm["date"])}</td></tr>'
                            )
                        sections.append(
                            f'<div class="wt-panel">'
                            f'<div class="wt-panel-head">Unpushed commits <span class="wt-count">{len(commits)}</span></div>'
                            f"<table><tr><th>SHA</th><th>Message</th><th>Author</th><th>Date</th></tr>"
                            f"{''.join(crows)}</table></div>"
                        )

                r_diff = self._worktree_git("diff", f"{bare_sha}..{selected}")
                if r_diff and r_diff.returncode == 0 and r_diff.stdout.strip():
                    sections.append(
                        f'<div class="wt-panel">'
                        f'<div class="wt-panel-head">Diff vs bare</div>'
                        f"{self._render_diff(r_diff.stdout)}</div>"
                    )
            else:
                sections.append(f'<p class="meta">Branch <strong>{e(selected)}</strong> not in bare repo</p>')

        if selected == current_branch or ref is None:
            if status_error:
                sections.append(f"<pre>{e(status_error)}</pre>")
            elif not status_lines:
                sections.append('<p class="meta">Working tree clean</p>')
            else:
                staged, unstaged, untracked, conflicts = [], [], [], []
                for line in status_lines:
                    if len(line) < 3:
                        continue
                    x, y = line[0], line[1]
                    path = line[3:]
                    if x == "?" and y == "?":
                        untracked.append(("?", path))
                    elif (x == "U" or y == "U") or (x == y == "A") or (x == y == "D"):
                        conflicts.append(("C", path))
                    else:
                        if x not in (" ", "?"):
                            staged.append((x, path))
                        if y not in (" ", "?"):
                            unstaged.append((y, path))

                def _file_list(title: str, items: list[tuple[str, str]]) -> str:
                    if not items:
                        return ""
                    files_html = ""
                    for s, p in items:
                        cls = "Q" if s == "?" else s
                        files_html += (
                            f'<div class="wt-file wt-s-{e(cls)}">'
                            f'<span class="wt-file-status">{e(s)}</span>'
                            f'<span class="wt-file-path">{e(p)}</span>'
                            f"</div>"
                        )
                    return (
                        f'<div class="wt-panel">'
                        f'<div class="wt-panel-head">{e(title)} <span class="wt-count">{len(items)}</span></div>'
                        f"{files_html}</div>"
                    )

                sections.append(_file_list("Staged", staged))
                sections.append(_file_list("Unstaged", unstaged))
                sections.append(_file_list("Conflicts", conflicts))
                sections.append(_file_list("Untracked", untracked))

            r_staged_diff = self._worktree_git("diff", "--cached")
            if r_staged_diff and r_staged_diff.returncode == 0 and r_staged_diff.stdout.strip():
                sections.append(
                    f'<div class="wt-panel">'
                    f'<div class="wt-panel-head">Staged changes</div>'
                    f"{self._render_diff(r_staged_diff.stdout)}</div>"
                )

            r_unstaged_diff = self._worktree_git("diff")
            if r_unstaged_diff and r_unstaged_diff.returncode == 0 and r_unstaged_diff.stdout.strip():
                sections.append(
                    f'<div class="wt-panel">'
                    f'<div class="wt-panel-head">Unstaged changes</div>'
                    f"{self._render_diff(r_unstaged_diff.stdout)}</div>"
                )

        content = "\n".join(s for s in sections if s)
        self._send(200, self._page("Worktree", content, "worktree"))


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------


class GitHubFrontend:
    """HTTP frontend for browsing MockGitHubClient state.

    Args:
        client: Single client or list of clients to browse.
        host: Bind address.
        port: Port (0 = auto-assign).
    """

    def __init__(
        self,
        client: MockGitHubClient | list[MockGitHubClient],
        host: str = "0.0.0.0",
        port: int = 0,
        worktree_path: str | None = None,
        detach: bool = False,
    ) -> None:
        if isinstance(client, list):
            self._clients = {(c.repo_owner, c.repo_name): c for c in client}
        else:
            self._clients = {(client.repo_owner, client.repo_name): client}
        self._host = host
        self._port = port
        self._worktree_path = worktree_path
        self._detach = detach and hasattr(os, "fork")
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._child_pid: int | None = None

    def start(self) -> None:
        """Start the HTTP server. Idempotent.

        When ``detach=True``, forks a child process so the server survives
        the parent exiting.  Otherwise uses a daemon thread (default).
        """
        if self._server is not None or self._child_pid is not None:
            return

        handler = type("Handler", (_GitHubHandler,), {"clients": self._clients, "worktree_path": self._worktree_path})
        self._server = HTTPServer((self._host, self._port), handler)
        self._port = self._server.server_address[1]

        if self._detach:
            pid = os.fork()
            if pid == 0:
                os.setsid()
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                self._server.serve_forever()
                os._exit(0)
            self._child_pid = pid
            self._server.server_close()
            self._server = None
        else:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

        logger.info("GitHub frontend started at %s", self.url)

    def stop(self) -> None:
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
                os.waitpid(self._child_pid, 0)
            except OSError:
                pass
            self._child_pid = None
            return
        if self._server is not None:
            self._server.shutdown()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server.server_close()
            self._server = None
            self._thread = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def __enter__(self) -> GitHubFrontend:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
