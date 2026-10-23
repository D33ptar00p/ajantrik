"""The studio: a local web UI that edits a repo's factory and drives its jobs.

    factory studio [--port 8765] [--no-browser]

Binds 127.0.0.1 only. Every start mints a random token; the printed URL carries it once, and the
first page load trades it for an HttpOnly, SameSite=Strict cookie. Every /api and /board request
needs that cookie or the X-Factory-Token header; requests that change anything need the header and
a JSON body (a cross-site form can send neither). Host headers other than 127.0.0.1/localhost on
this port are refused, which stops DNS-rebinding pages from reaching the API.

The server edits only .factory/config/ (through Project) and calls the `factory` CLI for render,
doctor and job actions, so the studio and the terminal always behave the same.
"""

from __future__ import annotations

import http.server
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ajantrik.core.connections import tickets as tix
from ajantrik.core.graph import DELIVER
from ajantrik.core.util import FactoryError, read_json
from ajantrik.kit import workspace
from ajantrik.kit.project import AgentDef, Project, SkillDef, guardrail_library, library_dir, list_presets
from ajantrik.kit.schema import BUILTIN_GUARDRAILS, FactoryConfig
from ajantrik.kit.workspace import Registry

WEB = Path(__file__).resolve().parent / "web"
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
JOB_ID = re.compile(r"^[0-9]{3}-[A-Za-z0-9_-]+$")
STAGE = re.compile(r"^[a-z][a-z0-9-]*$")
COOKIE = "factory_studio"
MAX_BODY = 2_000_000
CLI_TIMEOUT = 600
EARLY_EXIT = 3.0  # seconds a detached command is watched for an immediate failure before "started" is reported


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _cli_argv(args: list[str]) -> list[str]:
    return [sys.executable, "-m", "ajantrik.kit.cli", *args]


def _cli(root: Path, args: list[str], env: dict[str, str] | None = None, timeout: int = CLI_TIMEOUT) -> dict[str, Any]:
    """Run `python -m ajantrik.kit.cli <args>` in the repo. Returns {ok, data|output, error}."""
    full_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(_cli_argv(args), cwd=root, capture_output=True,
                              text=True, timeout=timeout, env=full_env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"factory {args[0]} timed out after {timeout}s"}
    out: dict[str, Any] = {"ok": proc.returncode == 0}
    text = proc.stdout.strip()
    try:
        out["data"] = json.loads(text) if text else None
    except json.JSONDecodeError:
        out["output"] = text
    if proc.returncode != 0:
        out["error"] = proc.stderr.strip() or text or f"factory {args[0]} exited {proc.returncode}"
    return out


def _library() -> dict[str, list[dict[str, Any]]]:
    lib = library_dir()
    agents, skills = [], []
    for f in sorted((lib / "agents").glob("*.md")) if (lib / "agents").is_dir() else []:
        try:
            a = AgentDef.parse(f.read_text(), f.stem)
            agents.append({"name": f.stem, "description": a.description})
        except ValueError:
            continue
    for f in sorted((lib / "skills").glob("*/SKILL.md")) if (lib / "skills").is_dir() else []:
        try:
            s = SkillDef.parse(f.read_text(), f.parent.name)
            skills.append({"name": f.parent.name, "description": s.description})
        except ValueError:
            continue
    guardrails = [{**g, "builtin": False} for g in guardrail_library()]
    builtins = [{"key": k, "title": b["title"], "description": b["description"], "flag": b["flag"], "cmd": b["cmd"],
                 "builtin": True} for k, b in BUILTIN_GUARDRAILS.items()]
    return {"agents": agents, "skills": skills, "guardrails": guardrails, "builtin_guardrails": builtins}


class Studio:
    """The API, independent of HTTP plumbing so tests can call it too."""

    def __init__(self, root: Path):
        self.project = Project(root)
        self.root = self.project.root
        self.lock = threading.Lock()

    # ---- reads ----------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        p = self.project
        try:
            raw = p.raw() if p.exists() else {}
        except Exception as e:
            raw = {}
            load_errors = [f"factory.yaml: {e}"]
        else:
            load_errors = []
        loaded = p.load() if p.exists() else None
        effective = loaded.cfg.model_dump(mode="json") if loaded and loaded.cfg else None
        return {
            "root": str(self.root),
            "config": raw,
            "effective": effective,
            "yaml": p.yaml_path.read_text() if p.exists() else "",
            "schema": FactoryConfig.model_json_schema(),
            "agents": [a.to_json() for a in (loaded.agents.values() if loaded else [])],
            "skills": [s.to_json() for s in (loaded.skills.values() if loaded else [])],
            "errors": load_errors + (loaded.errors if loaded else ["no .factory/config/factory.yaml"]),
            "warnings": loaded.warnings if loaded else [],
            "presets": list_presets(),
            "library": _library(),
        }

    def jobs(self) -> list[dict[str, Any]]:
        out = []
        jobs_dir = self.project.factory_dir / "jobs"
        for f in sorted(jobs_dir.glob("*/job.json"), reverse=True) if jobs_dir.is_dir() else []:
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    # ---- config ---------------------------------------------------------------------------------
    def save_config(self, body: dict[str, Any]) -> dict[str, Any]:
        cfg = body.get("config")
        if not isinstance(cfg, dict):
            raise ApiError(400, "body must be {config: {...}}")
        with self.lock:
            loaded = self.project.save_yaml(cfg)  # writes only when valid
            saved = self._is_saved(loaded)
            result: dict[str, Any] = {"saved": saved, "errors": loaded.errors, "warnings": loaded.warnings}
            if saved:
                result["render"] = self.render()
        return result

    @staticmethod
    def _is_saved(loaded: Any) -> bool:
        hard = [e for e in loaded.errors if "unknown agent" not in e and "unknown skill" not in e]
        return loaded.cfg is not None and not hard

    def render(self, check: bool = False) -> dict[str, Any]:
        return _cli(self.root, ["render", *(["--check"] if check else []), "--json"])

    # ---- agents and skills ----------------------------------------------------------------------
    @staticmethod
    def _name(name: str, what: str) -> str:
        if not isinstance(name, str) or not NAME.match(name):
            raise ApiError(400, f"{what} name must match {NAME.pattern}")
        return name

    def save_agent(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        self._name(name, "agent")
        new_name = self._name(body.get("name") or name, "agent")
        old = body.get("old_name")
        if old is not None:
            self._name(old, "agent")
        tools = body.get("tools") or []
        skills = body.get("skills") or []
        mcp = body.get("mcp") or []
        if not all(isinstance(x, str) for x in [*tools, *skills, *mcp]):
            raise ApiError(400, "tools, skills and mcp must be lists of strings")
        agent = AgentDef(name=new_name, description=str(body.get("description") or ""), tools=list(tools),
                         model=(body.get("model") or None), skills=list(skills), mcp=list(mcp),
                         body=str(body.get("body") or ""))
        with self.lock:
            if old and old != new_name and (self.project.agents_dir / f"{new_name}.md").exists():
                raise ApiError(409, f"agent {new_name} already exists")
            self.project.save_agent(agent, old_name=old)
        return self._after_edit()

    def delete_agent(self, name: str) -> dict[str, Any]:
        self._name(name, "agent")
        with self.lock:
            self.project.delete_agent(name)
        return self._after_edit()

    def agent_from_library(self, body: dict[str, Any]) -> dict[str, Any]:
        lib = self._name(body.get("library", ""), "library agent")
        name = self._name(body.get("name") or lib, "agent")
        src = library_dir() / "agents" / f"{lib}.md"
        if not src.is_file():
            raise ApiError(404, f"no library agent {lib}")
        a = AgentDef.parse(src.read_text(), lib)
        a.name = name
        with self.lock:
            if (self.project.agents_dir / f"{name}.md").exists():
                raise ApiError(409, f"agent {name} already exists")
            self.project.save_agent(a)
        return self._after_edit()

    def save_skill(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        self._name(name, "skill")
        new_name = self._name(body.get("name") or name, "skill")
        old = body.get("old_name")
        if old is not None:
            self._name(old, "skill")
        existing = self.project.skills().get(old or name)
        skill = SkillDef(name=new_name, description=str(body.get("description") or ""),
                         body=str(body.get("body") or ""), meta=dict(existing.meta) if existing else {})
        with self.lock:
            if old and old != new_name and (self.project.skills_dir / new_name).exists():
                raise ApiError(409, f"skill {new_name} already exists")
            self.project.save_skill(skill, old_name=old)
        return self._after_edit()

    def delete_skill(self, name: str) -> dict[str, Any]:
        self._name(name, "skill")
        with self.lock:
            self.project.delete_skill(name)
        return self._after_edit()

    def skill_from_library(self, body: dict[str, Any]) -> dict[str, Any]:
        lib = self._name(body.get("library", ""), "library skill")
        name = self._name(body.get("name") or lib, "skill")
        src = library_dir() / "skills" / lib
        if not (src / "SKILL.md").is_file():
            raise ApiError(404, f"no library skill {lib}")
        target = self.project.skills_dir / name
        with self.lock:
            if target.exists():
                raise ApiError(409, f"skill {name} already exists")
            shutil.copytree(src, target)
            if name != lib:
                s = SkillDef.parse((target / "SKILL.md").read_text(), name)
                s.name = name
                (target / "SKILL.md").write_text(s.to_markdown())
        return self._after_edit()

    def _after_edit(self) -> dict[str, Any]:
        loaded = self.project.load()
        out: dict[str, Any] = {"ok": True, "errors": loaded.errors, "warnings": loaded.warnings,
                               "agents": [a.to_json() for a in loaded.agents.values()],
                               "skills": [s.to_json() for s in loaded.skills.values()]}
        if self._is_saved(loaded):
            out["render"] = self.render()
        return out

    # ---- backlog --------------------------------------------------------------------------------
    def _backlog_cfg(self) -> tuple[str, bool]:
        """(key pattern, headless), read fresh every time: factory.yaml can change under a running studio."""
        cfg = self.project.load().cfg
        if cfg is None:
            return tix.KEY_PATTERN, False
        return cfg.connections.tickets.key_pattern, cfg.runtime.kind != "claude-code"

    def _jira(self) -> dict[str, Any]:
        """Whether the Backlog tab offers Sync with Jira, and whether that sync can also push."""
        cfg = self.project.load().cfg
        t = cfg.connections.tickets if cfg else None
        connected = bool(t and t.provider == "jira")
        return {"connected": connected, "push": bool(connected and t and t.mode == "rest" and t.project),
                "project": t.project if connected and t else None}

    def backlog(self) -> dict[str, Any]:
        pattern, headless = self._backlog_cfg()
        return {"items": tix.list_items(self.root, pattern), "headless": headless, "jira": self._jira()}

    def sync_backlog(self) -> dict[str, Any]:
        """Two-way sync: push queued items without a ticket to Jira (rest mode only), then pull tickets in."""
        jira = self._jira()
        if not jira["connected"]:
            raise ApiError(409, "Jira is not connected: set connections.tickets.provider to jira")
        with self.lock:
            r = _cli(self.root, ["sync", *(["--push"] if jira["push"] else []), "--json"], timeout=300)
            if not r["ok"]:
                raise ApiError(502, r.get("error") or "factory sync failed")
            report = (r.get("data") or {}).get("report") or []
            return {"report": report, "pushed": jira["push"], "items": tix.list_items(self.root, self._backlog_cfg()[0])}

    def _backlog_write(self, fn: Callable[..., list[dict[str, Any]]], *args: Any, **kw: Any) -> dict[str, Any]:
        with self.lock:
            try:
                return {"items": fn(self.root, *args, key_pattern=self._backlog_cfg()[0], **kw)}
            except tix.BacklogConflict as e:
                raise ApiError(409, str(e)) from e
            except FactoryError as e:
                raise ApiError(400, str(e)) from e

    @staticmethod
    def _index(raw: str) -> int:
        if not (raw.isascii() and raw.isdigit()):
            raise ApiError(400, "bad backlog index")
        return int(raw)

    @staticmethod
    def _str(body: dict[str, Any], k: str) -> str | None:
        v = body.get(k)
        if v is not None and not isinstance(v, str):
            raise ApiError(400, f"{k} must be a string")
        return v

    def _need(self, body: dict[str, Any], k: str) -> str:
        v = self._str(body, k)
        if v is None:
            raise ApiError(400, f"{k} is required")
        return v

    def add_backlog(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._backlog_write(tix.add_item, self._need(body, "text"), key=self._str(body, "key"),
                                   base=self._str(body, "base"))

    def update_backlog(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        i = self._index(index)
        return self._backlog_write(tix.update_item, i, self._need(body, "line"), text=self._need(body, "text"),
                                   key=self._str(body, "key"), base=self._str(body, "base"),
                                   state=self._need(body, "state"))

    def delete_backlog(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._backlog_write(tix.delete_item, self._index(index), self._need(body, "line"))

    def run_backlog(self, body: dict[str, Any]) -> dict[str, Any]:
        pattern, headless = self._backlog_cfg()
        if not headless:
            raise ApiError(409, "the runtime is interactive Claude Code: pick claude-cli, codex-cli or api "
                                "under Runtime to start jobs from here")
        raw = body.get("items")
        if not isinstance(raw, list) or not all(
                isinstance(it, dict) and type(it.get("index")) is int and isinstance(it.get("line"), str)
                for it in raw):
            raise ApiError(400, "items must be a list of {index, line}")
        picks = [(it["index"], it["line"]) for it in raw]
        with self.lock:
            try:
                result = tix.combine(tix.list_items(self.root, pattern), picks)
            except tix.BacklogConflict as e:
                raise ApiError(409, str(e)) from e
            except FactoryError as e:
                raise ApiError(400, str(e)) from e
        return self._detached(["next", "--items", ",".join(str(it["index"]) for it in result["items"])])

    # ---- jobs -----------------------------------------------------------------------------------
    @staticmethod
    def _job(job_id: str) -> str:
        if not JOB_ID.match(job_id):
            raise ApiError(400, "bad job id")
        return job_id

    def doctor(self, body: dict[str, Any]) -> dict[str, Any]:
        only = body.get("only")
        if only is not None and only not in ("git", "tickets", "runtime"):
            raise ApiError(400, "only must be git, tickets or runtime")
        return _cli(self.root, ["doctor", *(["--only", only] if only else []), "--json"], timeout=120)

    def new_job(self, body: dict[str, Any]) -> dict[str, Any]:
        request = str(body.get("request") or "").strip()
        ticket = str(body.get("ticket") or "").strip()
        base = str(body.get("base") or "").strip()
        if not request and not ticket:
            raise ApiError(400, "give a request or a ticket key")
        if ticket and not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", ticket):
            raise ApiError(400, "ticket must look like ABC-123")
        if base and not re.fullmatch(r"[\w./-]+", base):
            raise ApiError(400, "bad base branch name")
        args = ["run", request or ticket]
        if ticket:
            args += ["--ticket", ticket]
        if base:
            args += ["--base", base]
        return self._detached(args)

    def go(self, job_id: str) -> dict[str, Any]:
        return self._detached(["go", self._job(job_id)])

    def approve(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        args = ["approve", self._job(job_id)]
        stage = body.get("stage")
        if stage:
            if not isinstance(stage, str) or not STAGE.match(stage):
                raise ApiError(400, "bad stage id")
            args += ["--stage", stage]
        res = _cli(self.root, [*args, "--json"], env={"FACTORY_APPROVER": "studio"}, timeout=60)
        # An approved delivery goes straight on to deliver; nothing else would pick it up.
        if res.get("ok") and isinstance(res.get("data"), dict) and res["data"].get("stage") == DELIVER:
            return {**res, "delivering": self._detached(["deliver", job_id])}
        return res

    def deliver(self, job_id: str) -> dict[str, Any]:
        """Deliver a job; one waiting for its delivery approval is approved first (the studio's
        "approve & deliver"). The push and PR run in the background."""
        job_id = self._job(job_id)
        job = read_json(self.project.factory_dir / "jobs" / job_id / "job.json", {}) or {}
        if job.get("stage") == f"awaiting-{DELIVER}":
            res = _cli(self.root, ["approve", job_id, "--stage", DELIVER, "--json"], env={"FACTORY_APPROVER": "studio"},
                       timeout=60)
            if not res.get("ok"):
                return res
        return self._detached(["deliver", job_id])

    def _detached(self, args: list[str]) -> dict[str, Any]:
        """Long-running CLI commands (run, go) keep going after the request returns. One that fails within
        EARLY_EXIT seconds (no base branch, not rendered, bad config) is reported as an error instead."""
        logs = self.project.factory_dir / "jobs" / ".studio-runs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / f"{time.strftime('%Y%m%dT%H%M%S')}-{args[0]}.log"
        rel = str(log.relative_to(self.root))
        with log.open("w") as fh:
            proc = subprocess.Popen(_cli_argv(args), cwd=self.root, stdout=fh,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                                    env={**os.environ, "FACTORY_APPROVER": "studio"})
        try:
            code = proc.wait(timeout=EARLY_EXIT)
        except subprocess.TimeoutExpired:
            return {"ok": True, "started": True, "log": rel}
        if code:
            lines = [ln for ln in log.read_text(errors="replace").splitlines() if ln.strip()]
            raise ApiError(409, lines[-1] if lines else f"factory {args[0]} exited {code}", log=rel)
        return {"ok": True, "started": True, "finished": True, "log": rel}


def yaml_parse(body: dict[str, Any]) -> dict[str, Any]:
    """The Raw YAML tab edits text; the browser has no YAML parser, so the server converts."""
    import yaml
    text = body.get("text")
    if not isinstance(text, str):
        raise ApiError(400, "body must be {text: '...'}")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ApiError(400, f"YAML: {e}") from e
    if not isinstance(data, dict):
        raise ApiError(400, "factory.yaml must be a mapping")
    return {"config": data}


def yaml_dump(body: dict[str, Any]) -> dict[str, Any]:
    from ajantrik.kit.project import dump_yaml
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        raise ApiError(400, "body must be {config: {...}}")
    return {"text": dump_yaml(cfg)}


# ---- HTTP -----------------------------------------------------------------------------------------
class Workspace:
    """Every registered project, each with its own Studio (loaded on first use)."""

    def __init__(self, registry: Registry, default: str | None = None):
        self.registry = registry
        self.default = default  # the project `factory studio --root` was started for, if any
        self._studios: dict[str, Studio] = {}
        self.lock = threading.Lock()

    def studio(self, pid: str) -> Studio:
        if not isinstance(pid, str) or not NAME.match(pid):
            raise ApiError(400, "bad project id")
        try:
            entry = self.registry.get(pid)
        except FactoryError as e:
            raise ApiError(404, str(e)) from e
        with self.lock:
            st = self._studios.get(pid)
            if st is None or str(st.root) != entry["root"]:
                st = self._studios[pid] = Studio(Path(entry["root"]))
        return st

    def projects(self) -> list[dict[str, Any]]:
        return [workspace.summary(e) for e in self.registry.all()]

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        path = body.get("path") or body.get("root")
        if not isinstance(path, str) or not path.strip():
            raise ApiError(400, "path is required")
        api = body.get("api") if isinstance(body.get("api"), dict) else None
        if api and api.get("api_key_ref") and not re.match(r"^(env|keyring):[\w./-]+$", str(api["api_key_ref"])):
            raise ApiError(400, "api_key_ref must be a reference like env:NAME, never the key itself")
        try:
            return workspace.create(path.strip(), str(body.get("runtime") or "claude-code"),
                                    str(body.get("preset") or "blank"), (body.get("name") or None), api,
                                    registry=self.registry)
        except FactoryError as e:
            raise ApiError(400, str(e)) from e

    def remove(self, pid: str) -> dict[str, Any]:
        try:
            self.registry.remove(pid)
        except FactoryError as e:
            raise ApiError(404, str(e)) from e
        self._studios.pop(pid, None)
        return {"ok": True, "removed": pid}


def make_handler(ws: Workspace, token: str, port: int) -> type[http.server.BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "factory-studio"
        sys_version = ""

        def log_message(self, *args: Any) -> None:  # quiet: the board polls every 2 s
            pass

        # ---- helpers ------------------------------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _cookie_token(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return ""

        def _authed(self) -> bool:
            header = self.headers.get("X-Factory-Token", "")
            return secrets.compare_digest(header, token) or secrets.compare_digest(self._cookie_token(), token)

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "") in allowed_hosts

        def _body(self) -> dict[str, Any]:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise ApiError(415, "send application/json")
            if not secrets.compare_digest(self.headers.get("X-Factory-Token", ""), token):
                raise ApiError(403, "missing X-Factory-Token")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "body too large")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError as e:
                raise ApiError(400, "invalid JSON") from e
            if not isinstance(data, dict):
                raise ApiError(400, "body must be a JSON object")
            return data

        # ---- dispatch -----------------------------------------------------------------------
        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            if not self._host_ok():
                self._send(421, b"wrong host", "text/plain")
                return
            url = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(url.path)
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    self._index(url)
                    return
                if method == "GET" and path in ("/app.js", "/app.css"):
                    self._static(WEB / path.lstrip("/"))
                    return
                if not self._authed():
                    raise ApiError(401, "open the studio with the URL it printed (it carries the token)")
                # /p/<project>/board/... : that project's .factory/ (the dashboard and job files)
                m = re.match(r"^/p/([^/]+)/board(/.*)?$", path)
                if m:
                    if method != "GET":
                        raise ApiError(405, "read only")
                    prefix = f"/p/{m.group(1)}/board/"
                    if not m.group(2):
                        self._send(302, b"", "text/plain", {"Location": prefix + "dashboard.html"})
                        return
                    self._board(ws.studio(m.group(1)), m.group(2)[1:], prefix)
                    return
                if path == "/board" or path.startswith("/board/"):  # the default project (single-repo start)
                    if method != "GET":
                        raise ApiError(405, "read only")
                    if path == "/board":
                        self._send(302, b"", "text/plain", {"Location": "/board/dashboard.html"})
                        return
                    self._board(self._default(), path[len("/board/"):], "/board/")
                    return
                if path.startswith("/api/"):
                    parts = path[len("/api/"):].strip("/").split("/")
                    self._json(200, self._route(method, parts))
                    return
                raise ApiError(404, "not found")
            except ApiError as e:
                self._json(e.status, {"ok": False, "error": str(e), **e.extra})
            except Exception as e:  # report, don't crash the server thread
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        def _index(self, url: urllib.parse.SplitResult) -> None:
            q = urllib.parse.parse_qs(url.query)
            given = (q.get("token") or [""])[0]
            headers = {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                       "style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"}
            if given and secrets.compare_digest(given, token):
                headers["Set-Cookie"] = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"
                # Drop the token from the address bar and history.
                self._send(302, b"", "text/plain", {**headers, "Location": "/"})
                return
            if not self._authed():
                self._send(401, b"Open the studio with the URL it printed: it carries a one-time token.",
                           "text/plain; charset=utf-8")
                return
            html = (WEB / "index.html").read_text().replace("__FACTORY_TOKEN__", token)
            self._send(200, html.encode(), "text/html; charset=utf-8", headers)

        def _static(self, f: Path) -> None:
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            self._send(200, f.read_bytes(), ctype)

        def _default(self) -> Studio:
            if not ws.default:
                raise ApiError(404, "pick a project first")
            return ws.studio(ws.default)

        def _route(self, method: str, parts: list[str]) -> Any:
            head, rest = (parts[0] if parts else ""), parts[1:]
            if head == "projects":
                if method == "GET" and not rest:
                    return ws.projects()
                if method == "POST" and rest == ["inspect"]:
                    return workspace.inspect(str(self._body().get("path") or ""))
                if method == "POST" and not rest:
                    return ws.create(self._body())
                if method == "DELETE" and len(rest) == 1:
                    self._delete_check()
                    return ws.remove(rest[0])
                raise ApiError(404, f"no route {method} /api/{'/'.join(parts)}")
            if method == "GET" and parts == ["presets"]:
                return list_presets()
            if method == "GET" and parts == ["workspace"]:
                return {"default": ws.default, "home": str(ws.registry.path.parent),
                        "runtimes": workspace.RUNTIMES}
            if head == "p" and rest:
                return self._api(ws.studio(rest[0]), method, rest[1:])
            return self._api(self._default(), method, parts)

        def _board(self, studio: Studio, rel: str, prefix: str) -> None:
            base = studio.project.factory_dir.resolve()
            parts = [p for p in rel.split("/") if p]
            if any(p in ("..", ".") or p.startswith("~") for p in parts) or "\\" in rel or "\0" in rel:
                raise ApiError(400, "bad path")
            target = (base / "/".join(parts)).resolve() if parts else base
            if target != base and base not in target.parents:
                raise ApiError(400, "outside .factory")
            history = base / "config" / ".history"
            if target == history or history in target.parents:
                raise ApiError(404, "not found")
            if target.is_dir():
                if rel and not rel.endswith("/"):
                    self._send(301, b"", "text/plain", {"Location": f"{prefix}{rel}/"})
                    return
                index = target / "index.html"
                if index.is_file() and parts:
                    self._static(index)
                    return
                self._listing(target, rel)
                return
            if not target.is_file():
                raise ApiError(404, "not found")
            self._static(target)

        def _listing(self, d: Path, rel: str) -> None:
            """The same shape as http.server's listing: the dashboard parses the hrefs."""
            import html as h
            items = []
            for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
                if p.name == ".history":
                    continue
                name = p.name + ("/" if p.is_dir() else "")
                items.append(f'<li><a href="{urllib.parse.quote(name)}">{h.escape(name)}</a></li>')
            body = (f"<!DOCTYPE HTML><html><head><meta charset='utf-8'><title>Directory listing for /{h.escape(rel)}"
                    f"</title></head><body><h1>Directory listing for /{h.escape(rel)}</h1><hr><ul>"
                    + "\n".join(items) + "</ul><hr></body></html>")
            self._send(200, body.encode(), "text/html; charset=utf-8")

        def _api(self, studio: Studio, method: str, parts: list[str]) -> Any:
            head = parts[0] if parts else ""
            rest = parts[1:]
            if method == "GET" and head == "state":
                return studio.state()
            if method == "GET" and head == "jobs" and not rest:
                return studio.jobs()
            if method == "GET" and parts == ["backlog"]:
                return studio.backlog()
            if method == "GET" and parts == ["render", "check"]:
                return studio.render(check=True)
            if method in ("PUT", "POST", "DELETE"):
                body = self._body() if method != "DELETE" or head == "backlog" else self._delete_check()
                if method == "PUT" and parts == ["config"]:
                    return studio.save_config(body)
                if method == "POST" and parts == ["yaml", "parse"]:
                    return yaml_parse(body)
                if method == "POST" and parts == ["yaml", "dump"]:
                    return yaml_dump(body)
                if method == "POST" and parts == ["render"]:
                    return studio.render()
                if method == "POST" and parts == ["doctor"]:
                    return studio.doctor(body)
                if head == "agents":
                    if method == "POST" and rest == ["from-library"]:
                        return studio.agent_from_library(body)
                    if len(rest) == 1 and method == "PUT":
                        return studio.save_agent(rest[0], body)
                    if len(rest) == 1 and method == "DELETE":
                        return studio.delete_agent(rest[0])
                if head == "skills":
                    if method == "POST" and rest == ["from-library"]:
                        return studio.skill_from_library(body)
                    if len(rest) == 1 and method == "PUT":
                        return studio.save_skill(rest[0], body)
                    if len(rest) == 1 and method == "DELETE":
                        return studio.delete_skill(rest[0])
                if head == "backlog":
                    if method == "POST" and not rest:
                        return studio.add_backlog(body)
                    if method == "POST" and rest == ["run"]:
                        return studio.run_backlog(body)
                    if method == "POST" and rest == ["sync"]:
                        return studio.sync_backlog()
                    if len(rest) == 1 and method == "PUT":
                        return studio.update_backlog(rest[0], body)
                    if len(rest) == 1 and method == "DELETE":
                        return studio.delete_backlog(rest[0], body)
                if head == "jobs" and method == "POST":
                    if not rest:
                        return studio.new_job(body)
                    if len(rest) == 2 and rest[1] == "approve":
                        return studio.approve(rest[0], body)
                    if len(rest) == 2 and rest[1] == "deliver":
                        return studio.deliver(rest[0])
                    if len(rest) == 2 and rest[1] == "go":
                        return studio.go(rest[0])
            raise ApiError(404, f"no route {method} /api/{'/'.join(parts)}")

        def _delete_check(self) -> dict[str, Any]:
            if not secrets.compare_digest(self.headers.get("X-Factory-Token", ""), token):
                raise ApiError(403, "missing X-Factory-Token")
            return {}

    return Handler


def make_server(root: Path | None = None, port: int = 0,
                registry: Registry | None = None) -> tuple[http.server.ThreadingHTTPServer, str]:
    """All registered projects. With a root, that repo is registered (if it has a factory) and is the
    default project for the unprefixed /api and /board routes."""
    token = secrets.token_urlsafe(24)
    reg = registry or Registry()
    default = None
    if root is not None:
        entry = reg.find_root(root) or (reg.register(root) if Project(root).exists() else None)
        if entry is None:
            # not set up yet: serve it unregistered, so the studio can still show what is missing
            entry = {"id": "repo", "name": root.name, "root": str(root.resolve())}
            reg = _WithExtra(reg, entry)
        default = entry["id"]
    ws = Workspace(reg, default)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), http.server.BaseHTTPRequestHandler)
    actual = server.server_address[1]
    server.RequestHandlerClass = make_handler(ws, token, int(actual))
    server.daemon_threads = True
    return server, token


class _WithExtra(Registry):
    """A registry plus one in-memory project (a repo opened with --root before it has a factory)."""

    def __init__(self, base: Registry, extra: dict[str, Any]):
        super().__init__(base.path)
        self.extra = extra

    def all(self) -> list[dict[str, Any]]:
        return [*super().all(), self.extra]


def serve(root: Path | None = None, port: int = 8765, open_browser: bool = True) -> None:
    server, token = make_server(root, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    print(f"Ajantrik studio: {Project(root).root if root else 'all projects in ' + str(Registry().path)}")
    print(f"Ajantrik studio: open {url}")
    print("Ajantrik studio: Ctrl+C to stop", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="python -m ajantrik.studio.server")
    ap.add_argument("--root", default=".")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    serve(Path(a.root), a.port, not a.no_browser)
