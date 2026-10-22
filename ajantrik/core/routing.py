"""Which reviewers a diff needs. Deterministic, from config.

The hand-written factories asked the orchestrator to apply these rules by reading the diff; here
they are code, so every runtime routes the same way and the rules can be tested.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .graph import role_of
from .util import any_match, git, glob_match


def merge_base(worktree: Path, base: str) -> str:
    for ref in (base, f"origin/{base}"):
        mb = git("merge-base", ref, "HEAD", cwd=worktree, check=False)
        if mb:
            return mb
    return base


def changed_paths(worktree: Path, base: str) -> list[str]:
    out = git("diff", "--name-only", f"{merge_base(worktree, base)}...HEAD", cwd=worktree, check=False)
    return [p for p in out.splitlines() if p]


def added_lines(worktree: Path, base: str) -> dict[str, list[str]]:
    diff = git("diff", "-U0", "--no-color", f"{merge_base(worktree, base)}...HEAD", cwd=worktree, check=False)
    out: dict[str, list[str]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and path:
            out.setdefault(path, []).append(line[1:])
    return out


def reviewer_stages(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in cfg["loop"]["stages"] if role_of(s) == "reviewer"]


def is_routed(r: dict[str, Any]) -> bool:
    """A reviewer with triggers runs only when one matches; one without any always runs."""
    return bool(r.get("path_globs") or r.get("diff_patterns") or r.get("scoped_patterns") or r.get("metrics"))


def route_reviewers(cfg: dict[str, Any], paths: list[str], added: dict[str, list[str]],
                    gate: dict[str, Any] | None = None,
                    reviewers: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """reviewer stage id -> reason it runs, or "" when routing says it does not apply."""
    rvs = reviewer_stages(cfg) if reviewers is None else reviewers
    out = {r["id"]: "always" for r in rvs if not is_routed(r)}
    for r in [r for r in rvs if is_routed(r)]:
        skip = set(any_match(list(added), r.get("exclude_globs") or []))
        all_added = [(p, ln) for p, lines in added.items() if p not in skip for ln in lines]
        reason = ""
        excluded = set(any_match(paths, r.get("exclude_globs") or []))
        hit = [p for p in any_match(paths, r.get("path_globs") or []) if p not in excluded]
        if hit:
            reason = f"path {hit[0]}"
        if not reason:
            for pat in r.get("diff_patterns") or []:
                rx = re.compile(pat)
                m = next((p for p, ln in all_added if rx.search(ln)), None)
                if m:
                    reason = f"added line in {m} matches /{pat}/"
                    break
        if not reason:
            for sp in r.get("scoped_patterns") or []:
                rx = re.compile(sp["pattern"])
                m = next((p for p, ln in all_added if any(glob_match(p, g) for g in sp["paths"]) and rx.search(ln)),
                         None)
                if m:
                    reason = f"added line in {m} matches /{sp['pattern']}/"
                    break
        if not reason and gate:
            for rule in r.get("metrics") or []:
                v = gate.get(rule["key"])
                if isinstance(v, (int, float)) and v > rule["gt"]:
                    reason = f"{rule['key']}={v} > {rule['gt']}"
                    break
        out[r["id"]] = reason
    return out


def refused(cfg: dict[str, Any], paths: list[str], added: dict[str, list[str]]) -> str:
    """Why this diff needs a human security review before integration, or ""."""
    sec = cfg.get("security") or {}
    hit = any_match(paths, sec.get("refuse_paths") or [])
    if hit:
        return f"touches {hit[0]}"
    for sp in sec.get("refuse_patterns") or []:
        rx = re.compile(sp["pattern"])
        for p, lines in added.items():
            if any(glob_match(p, g) for g in sp["paths"]) and any(rx.search(ln) for ln in lines):
                return f"adds a line in {p} matching /{sp['pattern']}/"
    return ""
