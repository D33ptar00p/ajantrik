"""Projects: the repos this machine's Ajantrik knows about.

A project is one git repository with its own factory (.factory/config/: loop, agents, skills,
gate, connections, runtime). The registry only remembers where they are:

    $AJANTRIK_HOME/projects.json   (default ~/.ajantrik)   [{id, name, root, added}]

Adding a project that has no factory yet sets one up: the preset's config is copied into the
repo, the chosen runtime is written into it, and everything the runtimes need is rendered.
Removing a project only forgets it; the repo and its .factory/ stay as they are.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from ajantrik.core.util import FactoryError, read_json, write_json
from ajantrik.kit.project import Project, dump_yaml, list_presets
from ajantrik.kit.render import render

RUNTIMES = ["claude-code", "claude-cli", "codex-cli", "api"]
TERMINAL = {"ready", "delivered", "needs-human", "abandoned"}


def home() -> Path:
    return Path(os.environ.get("AJANTRIK_HOME") or Path.home() / ".ajantrik")


class Registry:
    def __init__(self, path: Path | None = None):
        self.path = path or home() / "projects.json"

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / ".projects.lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def all(self) -> list[dict[str, Any]]:
        data = read_json(self.path, {"projects": []}) or {}
        return [p for p in data.get("projects", []) if isinstance(p, dict) and p.get("id") and p.get("root")]

    def get(self, pid: str) -> dict[str, Any]:
        for p in self.all():
            if p["id"] == pid:
                return p
        raise FactoryError(f"no project {pid!r}")

    def find_root(self, root: Path) -> dict[str, Any] | None:
        r = str(root.resolve())
        return next((p for p in self.all() if p["root"] == r), None)

    def _save(self, projects: list[dict[str, Any]]) -> None:
        write_json(self.path, {"projects": projects})

    def register(self, root: Path, name: str | None = None) -> dict[str, Any]:
        root = root.resolve()
        with self._locked():
            projects = self.all()
            existing = next((p for p in projects if p["root"] == str(root)), None)
            if existing:
                return existing
            base = slugify(name or root.name)
            pid, n = base, 2
            while any(p["id"] == pid for p in projects):
                pid, n = f"{base}-{n}", n + 1
            entry = {"id": pid, "name": name or root.name, "root": str(root),
                     "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self._save([*projects, entry])
            return entry

    def remove(self, pid: str) -> None:
        with self._locked():
            projects = self.all()
            if not any(p["id"] == pid for p in projects):
                raise FactoryError(f"no project {pid!r}")
            self._save([p for p in projects if p["id"] != pid])


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "project"


# ---- creating ---------------------------------------------------------------------------------------
def inspect(path: str) -> dict[str, Any]:
    """What adding this path would do, before doing it."""
    root = Path(os.path.expanduser(path or "")).resolve() if path else None
    if root is None or not root.is_dir():
        return {"ok": False, "exists": False, "error": "no such folder"}
    git = (root / ".git").exists()
    configured = (root / ".factory" / "config" / "factory.yaml").is_file()
    info: dict[str, Any] = {"ok": git, "exists": True, "root": str(root), "git": git, "configured": configured,
                            "name": root.name, "registered": bool(Registry().find_root(root))}
    if not git:
        info["error"] = "not the root of a git repository"
    if configured:
        try:
            info["runtime"] = (Project(root).raw().get("runtime") or {}).get("kind", "claude-code")
        except (OSError, ValueError, yaml.YAMLError):
            pass
    return info


def create(path: str, runtime: str, preset: str = "blank", name: str | None = None,
           api: dict[str, Any] | None = None, registry: Registry | None = None) -> dict[str, Any]:
    """Register a repo as a project, setting up its factory if it has none."""
    reg = registry or Registry()
    info = inspect(path)
    if not info.get("exists"):
        raise FactoryError(f"{path}: no such folder")
    if not info.get("git"):
        raise FactoryError(f"{info['root']} is not the root of a git repository")
    if runtime not in RUNTIMES:
        raise FactoryError(f"runtime must be one of {RUNTIMES}")
    if preset not in [p["name"] for p in list_presets()]:
        raise FactoryError(f"no preset {preset!r}")
    root = Path(info["root"])
    p = Project(root)
    set_up = not info["configured"]
    if set_up:
        p.init_from_preset(preset, name=root.name)
        data = p.raw()
        rt = data.setdefault("runtime", {})
        rt["kind"] = runtime
        if runtime == "api" and api:
            rt["api"] = {k: v for k, v in {"provider": api.get("provider"), "base_url": api.get("base_url") or None,
                                           "api_key_ref": api.get("api_key_ref") or None}.items() if v}
            if api.get("model"):
                rt["models"] = {alias: api["model"] for alias in ("opus", "sonnet", "haiku")}
        loaded = p.save_yaml(data)
        if loaded.cfg is None:
            raise FactoryError("the new config is invalid: " + "; ".join(loaded.errors))
        gi = root / ".gitignore"
        lines = gi.read_text().splitlines() if gi.is_file() else []
        if ".factory/jobs/" not in lines:
            gi.write_text("\n".join([*lines, "", "# software factory runtime state", ".factory/jobs/"]).lstrip() + "\n")
    res = render(p)
    if res.errors:
        raise FactoryError("render failed: " + "; ".join(res.errors))
    entry = reg.register(root, name)
    return {"project": entry, "set_up": set_up, "preset": preset if set_up else None,
            "written": len(res.written), "warnings": res.warnings}


# ---- status ------------------------------------------------------------------------------------------
def summary(entry: dict[str, Any]) -> dict[str, Any]:
    """One project's status for the projects page: config health, backlog, jobs."""
    root = Path(entry["root"])
    out: dict[str, Any] = {**entry, "ok": True, "errors": []}
    if not root.is_dir():
        return {**out, "ok": False, "errors": [f"{root} is missing"]}
    p = Project(root)
    if not p.exists():
        return {**out, "ok": False, "errors": ["no .factory/config/factory.yaml (set it up again)"]}
    try:
        cfg = p.raw()
    except (OSError, ValueError, yaml.YAMLError) as e:
        return {**out, "ok": False, "errors": [f"factory.yaml: {e}"]}
    repo = cfg.get("repo") or {}
    out.update({"title": repo.get("title") or f"{repo.get('name', root.name)} Software Factory",
                "runtime": (cfg.get("runtime") or {}).get("kind", "claude-code"),
                "delivery": (cfg.get("delivery") or {}).get("mode", "none"),
                "base": repo.get("default_base", "main"),
                "tickets": ((cfg.get("connections") or {}).get("tickets") or {}).get("provider", "none")})
    if not (root / ".factory" / "factory.json").is_file():
        out["errors"].append("not rendered yet")
    # backlog
    text = (root / ".factory" / "backlog.md").read_text() if (root / ".factory" / "backlog.md").is_file() else ""
    queued = [ln[6:].strip() for ln in text.splitlines() if ln.startswith("- [ ] ")]
    blocked = [ln[6:].strip() for ln in text.splitlines() if ln.startswith("- [!] ")]
    out["backlog"] = {"queued": len(queued), "blocked": len(blocked), "next": queued[:3]}
    # jobs
    jobs = []
    for f in sorted((root / ".factory" / "jobs").glob("*/job.json"), reverse=True):
        j = read_json(f)
        if isinstance(j, dict):
            jobs.append(j)
    def brief(j: dict[str, Any]) -> dict[str, Any]:
        return {k: j.get(k) for k in ("id", "feature", "stage", "agent", "round", "note", "updated", "ticket")}
    active = [brief(j) for j in jobs if j.get("stage") not in TERMINAL and not str(j.get("stage", "")).startswith("awaiting-")]
    waiting = [brief(j) for j in jobs if str(j.get("stage", "")).startswith("awaiting-") or j.get("stage") == "needs-human"
               or (j.get("stage") == "ready")]
    counts: dict[str, int] = {}
    for j in jobs:
        counts[j.get("stage", "?")] = counts.get(j.get("stage", "?"), 0) + 1
    out["jobs"] = {"total": len(jobs), "active": active, "waiting": waiting,
                   "delivered": counts.get("delivered", 0), "abandoned": counts.get("abandoned", 0),
                   "last_updated": max((j.get("updated") or "" for j in jobs), default="")}
    return out


def set_runtime(root: Path, runtime: str) -> None:
    """Used by `factory projects add` on an existing factory when the user asks for another runtime."""
    p = Project(root)
    data = p.raw()
    data.setdefault("runtime", {})["kind"] = runtime
    p.yaml_path.write_text(dump_yaml(data))


