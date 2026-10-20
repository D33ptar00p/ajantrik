"""Small helpers shared by the runtime. Stdlib only: ajantrik/core is copied into each repo's .factory/lib."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


class FactoryError(Exception):
    """A rule or precondition stopped the factory; the message is for the human."""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(argv: list[str], cwd: Path | str | None = None, check: bool = True, env: dict[str, str] | None = None,
        timeout: float | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run without a shell. Raises FactoryError with stderr on failure when check is set."""
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", env=env, timeout=timeout,
                       input=input)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise FactoryError(f"`{' '.join(argv[:4])}` failed: {msg[-1] if msg else r.returncode}")
    return r


def git(*args: str, cwd: Path | str, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, check=check).stdout.strip()


def ref_exists(repo: Path, ref: str) -> bool:
    return run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=repo, check=False).returncode == 0


def write_json(path: Path, data: Any) -> None:
    """Atomic: the dashboard polls these files and must never read half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def first_line(path: Path) -> str:
    try:
        with path.open(errors="replace") as f:
            for line in f:
                if line.strip():
                    return line.strip()
    except OSError:
        pass
    return ""


def section_first_line(path: Path, heading: str) -> str:
    """First non-empty line under `## <heading>` (case-insensitive)."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    inside = False
    for line in lines:
        if line.startswith("#"):
            inside = line.lstrip("#").strip().lower() == heading.lower()
            continue
        if inside and line.strip():
            return line.strip()
    return ""


def slug(text: str, words: int = 4) -> str:
    parts = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"a", "an", "the", "to", "of", "and", "in", "on", "for", "with", "is", "be"}
    keep = [p for p in parts if p not in stop] or parts
    return "-".join(keep[:words]) or "job"


def underscore_words(text: str, words: int = 8) -> str:
    """`Add project rename` -> `Add_project_rename` (the hand-built factories' branch style)."""
    joined = "_".join(re.findall(r"[A-Za-z0-9]+", text)[:words])
    return (joined[:1].upper() + joined[1:]) or "Change"


def glob_match(path: str, pattern: str) -> bool:
    """Factory globs: `dir/` is a directory prefix, a pattern with / matches the full path, a bare
    pattern matches the file name (mc_calib*) or the full path."""
    path = path.replace("\\", "/")
    if pattern.endswith("/"):
        return path.startswith(pattern) or f"/{pattern}" in f"/{path}"
    if "/" in pattern:
        return fnmatch.fnmatchcase(path, pattern)  # fnmatch's * crosses /, so dir/** works too
    return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern) or fnmatch.fnmatchcase(path, pattern)


def any_match(paths: list[str], patterns: list[str]) -> list[str]:
    return [p for p in paths if any(glob_match(p, g) for g in patterns)]
