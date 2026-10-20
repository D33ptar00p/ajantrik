"""Git hosting: push, open a PR/MR, check the connection. GitHub and GitLab through their CLIs (gh,
glab, which keep their own login), Bitbucket through its REST API with a token reference.

Nothing here merges a PR. The one way commits reach the base branch is delivery.direct, which only
fast-forwards (or pushes a squash commit) after the configured approval.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..util import FactoryError, run
from .secrets import resolve


def _env(conn: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    token = resolve(conn.get("token_ref"), "git token")
    if token:
        if conn.get("provider") == "github":
            env["GH_TOKEN"] = token
        elif conn.get("provider") == "gitlab":
            env["GITLAB_TOKEN"] = token
    if conn.get("host"):
        env["GH_HOST" if conn.get("provider") == "github" else "GITLAB_HOST"] = conn["host"]
    return env


def push(worktree: Path, remote: str, src: str, dst: str, set_upstream: bool = False) -> str:
    argv = ["git", "-C", str(worktree), "push"] + (["-u"] if set_upstream else []) + [remote, f"{src}:{dst}"]
    r = run(argv, check=False)
    if r.returncode != 0:
        raise FactoryError(f"push to {remote} {dst} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return (r.stderr + r.stdout).strip()


def create_pr(conn: dict[str, Any], worktree: Path, base: str, branch: str, title: str, body_file: Path,
              pr: dict[str, Any]) -> str:
    provider = conn.get("provider")
    if provider == "github":
        argv = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", str(body_file)]
        argv += ["--draft"] if pr.get("draft") else []
        for r in pr.get("reviewers") or []:
            argv += ["--reviewer", r]
        for lbl in pr.get("labels") or []:
            argv += ["--label", lbl]
        out = run(argv, cwd=worktree, env=_env(conn)).stdout
        return _url(out)
    if provider == "gitlab":
        argv = ["glab", "mr", "create", "--source-branch", branch, "--target-branch", base, "--title", title,
                "--description", body_file.read_text(), "--yes", "--remove-source-branch"]
        argv += ["--draft"] if pr.get("draft") else []
        for r in pr.get("reviewers") or []:
            argv += ["--reviewer", r]
        if pr.get("labels"):
            argv += ["--label", ",".join(pr["labels"])]
        out = run(argv, cwd=worktree, env=_env(conn)).stdout
        return _url(out)
    if provider == "bitbucket":
        return _bitbucket_pr(conn, base, branch, title, body_file.read_text(), pr)
    raise FactoryError("delivery.mode=pr needs connections.git.provider github, gitlab or bitbucket")


def _url(text: str) -> str:
    m = re.findall(r"https?://\S+", text)
    return m[-1] if m else text.strip()


def _bitbucket_request(conn: dict[str, Any], method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    token = resolve(conn.get("token_ref"), "Bitbucket token")
    if not token:
        raise FactoryError("Bitbucket needs connections.git.token_ref (an access token)")
    url = f"https://api.bitbucket.org/2.0{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise FactoryError(f"Bitbucket {method} {path}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise FactoryError(f"Bitbucket {method} {path}: {e.reason}") from e


def _bitbucket_pr(conn: dict[str, Any], base: str, branch: str, title: str, body: str, pr: dict[str, Any]) -> str:
    ws, repo = conn.get("bitbucket_workspace"), conn.get("bitbucket_repo")
    if not ws or not repo:
        raise FactoryError("Bitbucket needs connections.git.bitbucket_workspace and bitbucket_repo")
    payload = {"title": title, "description": body, "source": {"branch": {"name": branch}},
               "destination": {"branch": {"name": base}}, "close_source_branch": True,
               "draft": bool(pr.get("draft"))}
    data = _bitbucket_request(conn, "POST", f"/repositories/{ws}/{repo}/pullrequests", payload)
    return ((data.get("links") or {}).get("html") or {}).get("href", "")


def pr_command(conn: dict[str, Any], worktree: str, base: str, branch: str, title_file: str, body_file: str) -> str:
    """The command a human runs for delivery.mode=none (shown on the dashboard and in the report)."""
    if conn.get("provider") == "gitlab":
        return (f'glab mr create --source-branch {branch} --target-branch {base} --title "$(cat {title_file})" '
                f'--description "$(cat {body_file})"')
    if conn.get("provider") == "github":
        return f'gh pr create --base {base} --head {branch} --title "$(cat {title_file})" --body-file {body_file}'
    return ""


def check(conn: dict[str, Any], root: Path) -> tuple[bool, str]:
    provider = conn.get("provider", "none")
    remote = conn.get("remote", "origin")
    url = run(["git", "remote", "get-url", remote], cwd=root, check=False).stdout.strip()
    if not url:
        return provider == "none", f"no git remote {remote!r}" + ("" if provider == "none" else " (needed to push)")
    reach = run(["git", "ls-remote", "--heads", remote], cwd=root, check=False, timeout=30)
    if reach.returncode != 0:
        return False, f"cannot reach {remote} ({url}): {(reach.stderr or '').strip()[-200:]}"
    detail = f"{remote} reachable ({_safe_url(url)})"
    try:
        if provider == "github":
            if not shutil.which("gh"):
                return False, detail + "; gh CLI not installed"
            r = run(["gh", "auth", "status"], env=_env(conn), check=False)
            return r.returncode == 0, detail + ("; gh logged in" if r.returncode == 0 else "; gh not logged in")
        if provider == "gitlab":
            if not shutil.which("glab"):
                return False, detail + "; glab CLI not installed"
            r = run(["glab", "auth", "status"], env=_env(conn), check=False)
            return r.returncode == 0, detail + ("; glab logged in" if r.returncode == 0 else "; glab not logged in")
        if provider == "bitbucket":
            ws, repo = conn.get("bitbucket_workspace"), conn.get("bitbucket_repo")
            _bitbucket_request(conn, "GET", f"/repositories/{ws}/{repo}")
            return True, detail + f"; Bitbucket API ok for {ws}/{repo}"
    except FactoryError as e:
        return False, f"{detail}; {e}"
    return True, detail


def _safe_url(url: str) -> str:
    """Never echo credentials embedded in a remote URL."""
    return re.sub(r"//[^/@]+@", "//***@", url)


def basic_auth(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
