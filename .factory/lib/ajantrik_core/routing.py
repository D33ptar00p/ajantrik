"""Which reviewers a diff needs, and which builder a finding belongs to. Deterministic, from config.

The hand-written factories asked the orchestrator to apply these rules by reading the diff; here
they are code, so every runtime routes the same way and the rules can be tested.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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


def route_reviewers(cfg: dict[str, Any], paths: list[str], added: dict[str, list[str]],
                    gate: dict[str, Any] | None = None) -> dict[str, str]:
    """reviewer id -> reason it runs, or "" when routing says it does not apply."""
    rv = cfg.get("reviewers") or {}
    out = {r["id"]: "always" for r in rv.get("always", [])}
    for r in rv.get("routed", []):
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


def reviewer_agent(cfg: dict[str, Any], rid: str) -> str:
    rv = cfg.get("reviewers") or {}
    for r in [*rv.get("always", []), *rv.get("routed", [])]:
        if r["id"] == rid:
            return r.get("agent") or f"reviewer-{rid}"
    return f"reviewer-{rid}"


def builders_for_paths(cfg: dict[str, Any], paths: list[str]) -> list[str]:
    """Builders whose area covers any of these paths; all builders when none claims them."""
    builders = cfg.get("builders") or []
    hit = [b["id"] for b in builders if any_match(paths, b.get("area_globs") or [])]
    return hit or [b["id"] for b in builders]


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
