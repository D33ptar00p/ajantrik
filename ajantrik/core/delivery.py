"""Delivery: what happens to a finished job's branch. The only code in the factory that writes to a
remote. Agents can't reach it: the guard blocks their git push / gh writes and their calls to
`factory deliver`; the engine calls it after the job is ready, the outgoing files passed their
secret scan, and (when configured) a human approved.

    none    stop at ready; print the push and PR commands for the human
    push    push the job branch
    pr      push, then open a PR (GitHub) or MR (GitLab) or PR (Bitbucket)
    direct  put the commits onto the base branch: ff-only (fails if the base moved) or one squash
            commit; pushed to the remote, or (push: false) the local base branch only
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .connections import git_host
from .util import FactoryError, first_line, git, ref_exists, run


def outgoing(job_dir: Path, names: list[str]) -> list[Path]:
    return [job_dir / n for n in names if (job_dir / n).is_file()]


def pr_title(job: dict[str, Any], job_dir: Path) -> str:
    return first_line(job_dir / "pr-title.txt") or job.get("feature", job["branch"])


def pr_body(job: dict[str, Any], job_dir: Path) -> Path:
    body = job_dir / "pr.md"
    if not body.is_file():
        body.write_text(f"{job.get('feature', '')}\n\nBuilt by the software factory, job {job['id']}.\n")
    return body


def deliver(cfg: dict[str, Any], job: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    d = cfg.get("delivery") or {}
    mode = d.get("mode", "none")
    conn = (cfg.get("connections") or {}).get("git") or {}
    remote = conn.get("remote", "origin")
    wt = Path(job["worktree"])
    branch, base = job["branch"], job["base"]
    if mode == "none":
        raise FactoryError("delivery.mode is none: the human pushes (see the commands on the dashboard)")
    if git("status", "--porcelain", cwd=wt, check=False):
        raise FactoryError(f"the worktree {wt} has uncommitted changes; commit or discard them first")

    if mode == "push":
        git_host.push(wt, remote, branch, branch, set_upstream=True)
        return {"mode": mode, "url": "", "detail": f"pushed {branch} to {remote}"}

    if mode == "pr":
        git_host.push(wt, remote, branch, branch, set_upstream=True)
        url = git_host.create_pr(conn, wt, base, branch, pr_title(job, job_dir), pr_body(job, job_dir),
                                 d.get("pr") or {})
        return {"mode": mode, "url": url, "detail": f"opened {url or 'a pull request'}"}

    if mode == "direct":
        opts = d.get("direct") or {}
        return _direct(wt, remote, branch, base, opts, job, job_dir)
    raise FactoryError(f"unknown delivery mode {mode!r}")


def _checked_out_anywhere(wt: Path, branch: str) -> list[str]:
    out = git("worktree", "list", "--porcelain", cwd=wt, check=False)
    paths, current = [], ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = line.split(" ", 1)[1]
        elif line == f"branch refs/heads/{branch}":
            paths.append(current)
    return paths


def _direct(wt: Path, remote: str, branch: str, base: str, opts: dict[str, Any], job: dict[str, Any],
            job_dir: Path) -> dict[str, Any]:
    push = opts.get("push", True)
    strategy = opts.get("strategy", "ff-only")
    if push:
        run(["git", "-C", str(wt), "fetch", remote, base], timeout=180)
        target = f"{remote}/{base}"
    else:
        target = base
    if not ref_exists(wt, target):
        raise FactoryError(f"{target} does not exist")

    if strategy == "ff-only":
        if run(["git", "-C", str(wt), "merge-base", "--is-ancestor", target, "HEAD"], check=False).returncode != 0:
            raise FactoryError(f"{target} moved since the job branched; rework the job onto it (or use squash)")
        head = git("rev-parse", "HEAD", cwd=wt)
        if push:
            git_host.push(wt, remote, head, f"refs/heads/{base}")
            return {"mode": "direct", "url": "", "detail": f"fast-forwarded {remote}/{base} to {head[:10]}"}
        return _move_local_base(wt, base, head)

    if strategy == "squash":
        message = pr_title(job, job_dir) + "\n\n" + (job_dir / "pr.md").read_text() if (job_dir / "pr.md").is_file() \
            else pr_title(job, job_dir)
        with tempfile.TemporaryDirectory(prefix="factory-squash-") as tmp:
            scratch = Path(tmp) / "wt"
            run(["git", "-C", str(wt), "worktree", "add", "--detach", str(scratch), target])
            try:
                r = run(["git", "-C", str(scratch), "merge", "--squash", branch], check=False)
                if r.returncode != 0:
                    raise FactoryError(f"squash onto {target} conflicts; rework the job onto it")
                # Pre-commit hooks run here as on any commit; a failing hook stops delivery.
                run(["git", "-C", str(scratch), "commit", "-F", "-"], input=message)
                head = git("rev-parse", "HEAD", cwd=scratch)
            finally:
                run(["git", "-C", str(wt), "worktree", "remove", "--force", str(scratch)], check=False)
        if push:
            git_host.push(wt, remote, head, f"refs/heads/{base}")
            return {"mode": "direct", "url": "", "detail": f"pushed squash commit {head[:10]} to {remote}/{base}"}
        return _move_local_base(wt, base, head)
    raise FactoryError(f"unknown direct strategy {strategy!r}")


def _move_local_base(wt: Path, base: str, head: str) -> dict[str, Any]:
    holders = _checked_out_anywhere(wt, base)
    if holders:
        raise FactoryError(f"{base} is checked out in {holders[0]}; moving it would leave that checkout stale. "
                           "Use direct.push: true, or switch that checkout away from it.")
    if run(["git", "-C", str(wt), "merge-base", "--is-ancestor", base, head], check=False).returncode != 0:
        raise FactoryError(f"{head[:10]} does not fast-forward {base}")
    git("branch", "-f", base, head, cwd=wt)
    return {"mode": "direct", "url": "", "detail": f"moved local {base} to {head[:10]}"}


def human_commands(cfg: dict[str, Any], job: dict[str, Any], job_dir: Path) -> list[str]:
    conn = (cfg.get("connections") or {}).get("git") or {}
    remote = conn.get("remote", "origin")
    cmds = [f"git -C {job['worktree']} push -u {remote} {job['branch']}"]
    pr = git_host.pr_command(conn, job["worktree"], job["base"], job["branch"], str(job_dir / "pr-title.txt"),
                             str(job_dir / "pr.md"))
    return cmds + ([pr] if pr else [])
