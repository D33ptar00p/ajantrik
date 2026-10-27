from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from ajantrik.kit.project import Project
from ajantrik.kit.render import render

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com"}


def sh(*argv: str, cwd: Path) -> str:
    import os

    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A tiny Python project with a bare `origin` remote next to it."""
    repo = tmp_path / "demo"
    repo.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text("from app.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-qm", "init", cwd=repo)
    origin = tmp_path / "origin.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    sh("git", "remote", "add", "origin", str(origin), cwd=repo)
    sh("git", "push", "-q", "origin", "main", cwd=repo)
    return repo


@pytest.fixture
def factory(git_repo: Path) -> Callable[..., Project]:
    """Initialise the default preset, apply overrides to factory.yaml, render."""

    def make(**overrides: Any) -> Project:
        p = Project(git_repo)
        p.init_from_preset("default", name="demo", force=True)
        data = p.raw()
        for key, value in overrides.items():
            target = data
            parts = key.split("__")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        p.yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))
        res = render(p)
        assert not res.errors, res.errors
        return p

    return make


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tests never touch the real ~/.ajantrik project registry."""
    home = tmp_path_factory.mktemp("ajantrik-home")
    monkeypatch.setenv("AJANTRIK_HOME", str(home))
    return home


FAKE_SRT = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
assert args[0] == "--settings", args
settings = json.load(open(args[1]))
# Like the real srt (0.0.81): these lists must be present, even empty.
for section, keys in {"network": ("allowedDomains", "deniedDomains"),
                      "filesystem": ("denyRead", "allowRead", "allowWrite", "denyWrite")}.items():
    missing = [k for k in keys if k not in settings.get(section, {})]
    if missing:
        sys.exit(f"Error: {args[1]} does not hold a valid config: {section}.{missing[0]}: Required")
log = os.environ.get("FAKE_SRT_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"settings": json.load(open(args[1])), "argv": args[2:], "cwd": os.getcwd()}) + "\\n")
os.execvp(args[2], args[2:])
"""


@pytest.fixture(autouse=True)
def fake_srt(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The sandbox is on by default and fails closed. Tests get a pass-through `srt` that logs each call
    (settings, argv, cwd) to the returned file; the live tests (-m live) use the real one."""
    from ajantrik.core import sandbox

    bin_dir = tmp_path_factory.mktemp("srt-bin")
    srt = bin_dir / "srt"
    srt.write_text(FAKE_SRT)
    srt.chmod(0o755)
    log = bin_dir / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SRT_LOG", str(log))
    monkeypatch.setattr(sandbox, "PLATFORM_DEPS", {})
    return log
