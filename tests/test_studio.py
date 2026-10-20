"""The studio's HTTP API: auth, host checks, config and agent edits, the /board file server."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import pytest
import yaml

from ajantrik.kit.project import Project, presets_dir
from ajantrik.studio import server as studio_server

MINIMAL = {
    "repo": {"name": "demo"},
    "loop": {"stages": [
        {"id": "spec", "kind": "agent", "agent": "spec-writer", "output": "spec.md"},
        {"id": "approval", "kind": "approval"},
    ]},
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    p = Project(tmp_path)
    if (presets_dir() / "blank" / "factory.yaml").is_file():
        try:
            p.init_from_preset("blank", name="demo")
        except Exception:
            p = _minimal(p)
        if not p.load().ok:
            p = _minimal(p)
    else:
        p = _minimal(p)
    (tmp_path / ".factory" / "dashboard.html").write_text('<meta name="factory-dashboard" content="1">')
    (tmp_path / ".factory" / "jobs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _minimal(p: Project) -> Project:
    import shutil
    shutil.rmtree(p.config_dir, ignore_errors=True)
    p.config_dir.mkdir(parents=True)
    p.yaml_path.write_text(yaml.safe_dump(MINIMAL))
    p.agents_dir.mkdir()
    (p.agents_dir / "spec-writer.md").write_text("---\nname: spec-writer\ndescription: writes specs\n---\n\nWrite spec.md.\n")
    return p


@pytest.fixture
def studio(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # Render shells out to the CLI, which other work may still be building; stub it here.
    monkeypatch.setattr(studio_server, "_cli", lambda root, args, env=None, timeout=0: {"ok": True, "data": {"args": args}})
    server, token = studio_server.make_server(repo, 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port, token, repo
    server.shutdown()
    server.server_close()


def call(port: int, method: str, path: str, token: str | None = None, body=None, host: str | None = None,
         ctype: str = "application/json", cookie: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Factory-Token"] = token
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = ctype
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    hdrs = dict(res.getheaders())
    conn.close()
    try:
        return res.status, json.loads(raw), hdrs
    except json.JSONDecodeError:
        return res.status, raw.decode(errors="replace"), hdrs


def test_token_required(studio):
    port, token, _ = studio
    assert call(port, "GET", "/api/state")[0] == 401
    assert call(port, "GET", "/api/state", token="wrong")[0] == 401
    assert call(port, "GET", "/board/dashboard.html")[0] == 401
    assert call(port, "GET", "/")[0] == 401
    assert call(port, "GET", "/api/state", token=token)[0] == 200


def test_token_in_url_becomes_cookie(studio):
    port, token, _ = studio
    status, _, headers = call(port, "GET", f"/?token={token}")
    assert status == 302 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    jar = cookie.split(";")[0]
    status, body, _ = call(port, "GET", "/", cookie=jar)
    assert status == 200 and token in body  # the page needs it for the CSRF header
    assert call(port, "GET", "/api/state", cookie=jar)[0] == 200
    # A cookie alone can't change anything: writes need the header too.
    assert call(port, "PUT", "/api/config", cookie=jar, body={"config": MINIMAL})[0] == 403


def test_wrong_host_rejected(studio):
    port, token, _ = studio
    assert call(port, "GET", "/api/state", token=token, host="evil.example:80")[0] == 421
    assert call(port, "GET", "/api/state", token=token, host=f"localhost:{port}")[0] == 200


def test_state_shape(studio):
    port, token, repo = studio
    status, st, _ = call(port, "GET", "/api/state", token=token)
    assert status == 200
    for key in ("root", "config", "effective", "yaml", "schema", "agents", "skills", "errors", "warnings",
                "presets", "library"):
        assert key in st
    assert st["root"] == str(repo.resolve())
    assert st["config"]["repo"]["name"] == "demo"
    assert st["effective"]["repo"]["title"] == "demo Software Factory"
    assert st["schema"]["title"] == "FactoryConfig"
    assert {"agents", "skills"} <= set(st["library"])
    assert any(a["name"] == "spec-writer" for a in st["agents"])


def test_config_put_validates(studio):
    port, token, repo = studio
    p = Project(repo)
    before = p.yaml_path.read_text()
    bad = {"repo": {"name": "demo"}, "loop": {"max_rounds": 99}}
    status, r, _ = call(port, "PUT", "/api/config", token=token, body={"config": bad})
    assert status == 200 and r["saved"] is False and r["errors"]
    assert p.yaml_path.read_text() == before
    secret = {"repo": {"name": "demo"}, "connections": {"tickets": {"token_ref": "plain-secret-value"}}}
    status, r, _ = call(port, "PUT", "/api/config", token=token, body={"config": secret})
    assert r["saved"] is False and any("token_ref" in e for e in r["errors"])

    good = json.loads(json.dumps(MINIMAL))
    good["loop"]["max_rounds"] = 5
    status, r, _ = call(port, "PUT", "/api/config", token=token, body={"config": good})
    assert status == 200 and r["saved"] is True, r
    assert yaml.safe_load(p.yaml_path.read_text())["loop"]["max_rounds"] == 5
    assert r["render"]["ok"] is True  # stubbed CLI
    assert list((p.config_dir / ".history").glob("factory.yaml.*"))


def test_config_put_needs_json(studio):
    port, token, _ = studio
    assert call(port, "PUT", "/api/config", token=token, body={"config": MINIMAL}, ctype="text/plain")[0] == 415
    assert call(port, "PUT", "/api/config", token=token, body={"nope": 1})[0] == 400


def test_agent_create_rename_delete(studio):
    port, token, repo = studio
    agents = Project(repo).agents_dir
    body = {"name": "reviewer-docs", "description": "reviews docs", "tools": ["Read", "Grep"], "model": "haiku",
            "skills": [], "mcp": [], "body": "Review the docs."}
    status, r, _ = call(port, "PUT", "/api/agents/reviewer-docs", token=token, body=body)
    assert status == 200 and r["ok"]
    text = (agents / "reviewer-docs.md").read_text()
    assert "model: haiku" in text and "Review the docs." in text

    status, r, _ = call(port, "PUT", "/api/agents/reviewer-docs", token=token,
                        body={**body, "name": "reviewer-documentation", "old_name": "reviewer-docs"})
    assert status == 200
    assert not (agents / "reviewer-docs.md").exists() and (agents / "reviewer-documentation.md").exists()

    status, r, _ = call(port, "DELETE", "/api/agents/reviewer-documentation", token=token)
    assert status == 200 and not (agents / "reviewer-documentation.md").exists()
    assert call(port, "DELETE", "/api/agents/spec-writer")[0] == 401


def test_skill_create_and_delete(studio):
    port, token, repo = studio
    status, _, _ = call(port, "PUT", "/api/skills/api-rules", token=token,
                        body={"name": "api-rules", "description": "API rules", "body": "## Rules\n\n- be nice\n"})
    assert status == 200
    assert (Project(repo).skills_dir / "api-rules" / "SKILL.md").is_file()
    assert call(port, "DELETE", "/api/skills/api-rules", token=token)[0] == 200
    assert not (Project(repo).skills_dir / "api-rules").exists()


@pytest.mark.parametrize("path", [
    "/api/agents/..%2F..%2Fetc",
    "/api/agents/Bad%20Name",
    "/api/skills/..",
])
def test_bad_names_rejected(studio, path):
    port, token, _ = studio
    status, _, _ = call(port, "PUT", path, token=token, body={"name": "x", "body": ""})
    assert status in (400, 404)


def test_rename_to_bad_name_rejected(studio):
    port, token, _ = studio
    status, _r, _ = call(port, "PUT", "/api/agents/spec-writer", token=token,
                        body={"name": "../../evil", "old_name": "spec-writer", "body": "x"})
    assert status == 400


@pytest.mark.parametrize("path", [
    "/board/../config/factory.yaml",
    "/board/%2e%2e/%2e%2e/etc/passwd",
    "/board/config/.history/",
    "/board/..%2F..%2Fetc%2Fpasswd",
])
def test_board_traversal_rejected(studio, path):
    port, token, _ = studio
    status, body, _ = call(port, "GET", path, token=token)
    assert status in (400, 404), (status, body)


def test_board_serves_factory_files(studio):
    port, token, repo = studio
    (repo / ".factory" / "jobs" / "001-demo").mkdir(parents=True)
    (repo / ".factory" / "jobs" / "001-demo" / "job.json").write_text('{"id": "001-demo", "stage": "spec"}')
    status, body, _ = call(port, "GET", "/board/dashboard.html", token=token)
    assert status == 200 and 'factory-dashboard' in body
    status, listing, _ = call(port, "GET", "/board/jobs/", token=token)
    assert status == 200 and 'href="001-demo/"' in listing
    status, job, _ = call(port, "GET", "/board/jobs/001-demo/job.json", token=token)
    assert status == 200 and job["id"] == "001-demo"
    assert call(port, "POST", "/board/jobs/", token=token, body={})[0] == 405


def test_jobs_list_and_actions(studio):
    port, token, repo = studio
    d = repo / ".factory" / "jobs" / "002-thing"
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"id": "002-thing", "stage": "awaiting-approval"}))
    status, jobs, _ = call(port, "GET", "/api/jobs", token=token)
    assert status == 200 and jobs[0]["id"] == "002-thing"
    status, r, _ = call(port, "POST", "/api/jobs/002-thing/approve", token=token, body={"stage": "approval"})
    assert status == 200 and r["data"]["args"][:4] == ["approve", "002-thing", "--stage", "approval"]
    assert call(port, "POST", "/api/jobs/..%2Fx/approve", token=token, body={})[0] in (400, 404)
    assert call(port, "POST", "/api/jobs/002-thing/approve", token=token, body={"stage": "x y"})[0] == 400
    assert call(port, "POST", "/api/jobs", token=token, body={"request": "", "ticket": ""})[0] == 400
    assert call(port, "POST", "/api/jobs", token=token, body={"request": "x", "ticket": "not a key"})[0] == 400


def test_job_start_reports_an_early_failure(studio):
    port, token, repo = studio  # the fixture repo cannot run headless, so `factory run` fails at once
    status, r, _ = call(port, "POST", "/api/jobs", token=token, body={"request": "x"})
    assert status == 409 and r["error"].startswith("factory: ")
    assert (repo / r["log"]).is_file()


def test_job_start_returns_while_still_running(studio, monkeypatch: pytest.MonkeyPatch):
    port, token, _ = studio
    monkeypatch.setattr(studio_server, "EARLY_EXIT", 0.2)
    monkeypatch.setattr(studio_server, "_cli_argv", lambda args: [sys.executable, "-c", "import time; time.sleep(2)"])
    status, r, _ = call(port, "POST", "/api/jobs", token=token, body={"request": "x"})
    assert status == 200 and r["started"] and "finished" not in r


def test_yaml_round_trip(studio):
    port, token, _ = studio
    status, r, _ = call(port, "POST", "/api/yaml/dump", token=token, body={"config": MINIMAL})
    assert status == 200 and "repo:" in r["text"]
    status, back, _ = call(port, "POST", "/api/yaml/parse", token=token, body={"text": r["text"]})
    assert back["config"] == MINIMAL
    assert call(port, "POST", "/api/yaml/parse", token=token, body={"text": "- a list"})[0] == 400


def test_static_assets_public(studio):
    port, _, _ = studio
    status, body, _headers = call(port, "GET", "/app.js")
    assert status == 200 and "Factory Studio" in body
    assert call(port, "GET", "/app.css")[0] == 200
