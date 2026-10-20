"""Projects: the registry, setting a repo up, summaries, and the studio's per-project routes."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ajantrik.core.util import FactoryError
from ajantrik.kit import workspace
from ajantrik.kit.project import Project
from ajantrik.studio.server import make_server

from .conftest import sh


def new_repo(base: Path, name: str) -> Path:
    repo = base / name
    repo.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("# x\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-qm", "init", cwd=repo)
    return repo


def test_create_sets_up_a_repo_with_the_chosen_runtime(tmp_path):
    repo = new_repo(tmp_path, "shop")
    info = workspace.inspect(str(repo))
    assert info["git"] and not info["configured"] and not info["registered"]
    res = workspace.create(str(repo), "api", api={"provider": "ollama", "model": "llama3.2"})
    assert res["set_up"] and res["written"] > 20
    cfg = Project(repo).raw()
    assert cfg["runtime"]["kind"] == "api" and cfg["runtime"]["api"] == {"provider": "ollama"}
    assert cfg["runtime"]["models"]["opus"] == "llama3.2"
    for f in (".factory/factory.json", ".factory/factory.py", ".claude/skills/factory/SKILL.md", ".claude/settings.json"):
        assert (repo / f).is_file(), f
    assert ".factory/jobs/" in (repo / ".gitignore").read_text()
    assert workspace.Registry().all()[0]["id"] == "shop"
    # adding again: no second entry, nothing re-initialised
    again = workspace.create(str(repo), "codex-cli")
    assert not again["set_up"] and len(workspace.Registry().all()) == 1
    assert Project(repo).raw()["runtime"]["kind"] == "api"


def test_create_refuses_non_repos_and_bad_runtimes(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(FactoryError, match="git repository"):
        workspace.create(str(plain), "claude-code")
    with pytest.raises(FactoryError, match="runtime"):
        workspace.create(str(new_repo(tmp_path, "r")), "gpt-cli")
    assert workspace.inspect(str(tmp_path / "nope"))["exists"] is False


def test_ids_are_unique_and_remove_only_forgets(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    a, b = new_repo(tmp_path / "x", "app"), new_repo(tmp_path / "y", "app")
    ids = [workspace.create(str(r), "claude-code")["project"]["id"] for r in (a, b)]
    assert ids == ["app", "app-2"]
    workspace.Registry().remove("app")
    assert [p["id"] for p in workspace.Registry().all()] == ["app-2"]
    assert (a / ".factory" / "config" / "factory.yaml").is_file()


def test_summary_counts_jobs_and_backlog(tmp_path):
    repo = new_repo(tmp_path, "svc")
    entry = workspace.create(str(repo), "claude-cli")["project"]
    jobs = repo / ".factory" / "jobs"
    for jid, stage in [("001-a", "build"), ("002-b", "awaiting-approval"), ("003-c", "delivered"), ("004-d", "ready")]:
        (jobs / jid).mkdir(parents=True)
        (jobs / jid / "job.json").write_text(json.dumps({"id": jid, "stage": stage, "feature": jid, "updated": "2026-10-20T00:00:00Z"}))
    (repo / ".factory" / "backlog.md").write_text("## Queue\n\n- [ ] base: main · one\n- [ ] base: main · two\n- [!] X-1 · blocked · three\n")
    s = workspace.summary(entry)
    assert s["ok"] and s["runtime"] == "claude-cli"
    assert [j["id"] for j in s["jobs"]["active"]] == ["001-a"]
    assert {j["id"] for j in s["jobs"]["waiting"]} == {"002-b", "004-d"}
    assert s["jobs"]["delivered"] == 1
    assert s["backlog"] == {"queued": 2, "blocked": 1, "next": ["base: main · one", "base: main · two"]}


def test_studio_serves_every_project_under_its_own_prefix(tmp_path):
    a, b = new_repo(tmp_path, "alpha"), new_repo(tmp_path, "beta")
    workspace.create(str(a), "claude-code")
    srv, token = make_server(None, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Factory-Token": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        code, projects = call("GET", "/api/projects")
        assert code == 200 and [p["id"] for p in projects] == ["alpha"]
        code, info = call("POST", "/api/projects/inspect", {"path": str(b)})
        assert code == 200 and info["git"] and not info["configured"]
        code, res = call("POST", "/api/projects", {"path": str(b), "runtime": "codex-cli", "preset": "blank"})
        assert code == 200 and res["set_up"] and res["project"]["id"] == "beta"
        code, res = call("POST", "/api/projects", {"path": str(b), "runtime": "api", "api": {"api_key_ref": "sk-live-123"}})
        assert code == 400 and "reference" in res["error"]
        code, st = call("GET", "/api/p/beta/state")
        assert code == 200 and st["root"] == str(b) and st["effective"]["runtime"]["kind"] == "codex-cli"
        code, st = call("GET", "/api/p/alpha/state")
        assert st["root"] == str(a)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/p/beta/board/dashboard.html",
                                     headers={"X-Factory-Token": token})
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 200 and b'factory-dashboard' in r.read()
        assert call("GET", "/api/p/nope/state")[0] == 404
        assert call("GET", "/api/p/..%2f/state")[0] in (400, 404)
        assert call("GET", "/api/state")[0] == 404  # no default project in all-projects mode
        assert call("DELETE", "/api/projects/alpha")[0] == 200
        assert [p["id"] for p in call("GET", "/api/projects")[1]] == ["beta"]
    finally:
        srv.shutdown()
