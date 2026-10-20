"""`factory`: Ajantrik, the software factory. Set up, edit, check and run it.

    factory init [--preset NAME] [--name REPO] [--force]   create .factory/config/ from a preset, then render
    factory presets                                       list presets
    factory validate                                      check the config (errors and warnings)
    factory render [--check]                              write .claude/ and .factory/ from the config
    factory studio [--port N] [--no-browser]              the web studio: every project (or this repo's, inside one)
    factory projects                                      list projects (repos this machine's Ajantrik knows)
    factory projects add <path> [--runtime R] [--preset P] [--name N]   register a repo, setting up its factory
    factory projects remove <id>                          forget a project (its files stay)
    factory doctor [--only git|tickets|runtime]           check connections and the runtime
    factory run "<request>" [--ticket K] [--base B] [--wait]   headless: create a job and drive it
    factory go <job> [--wait]                             headless: approve the waiting stage and continue
    factory resume <job> [--wait]                         headless: continue a job
    factory new|step|advance|refresh|gate|approve|rework|deliver|abandon|status|sync|next|route|ticket
                                                          job commands (same as python3 .factory/factory.py)

`--json` (anywhere on the line) prints machine-readable output; the studio relies on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ajantrik.core import cli as core_cli
from ajantrik.core.util import FactoryError
from ajantrik.kit.project import Project, list_presets
from ajantrik.kit.render import render

JOB_COMMANDS = {"new", "step", "advance", "refresh", "gate", "approve", "rework", "deliver", "abandon", "status",
                "sync", "route", "ticket", "gate-run"}


def _project(args: argparse.Namespace) -> Project:
    if getattr(args, "root", None):
        return Project(Path(args.root))
    return Project.find()


def cmd_init(args: argparse.Namespace) -> Any:
    root = Path(args.root or ".").resolve()
    if not (root / ".git").exists():
        raise FactoryError(f"{root} is not the root of a git repository")
    p = Project(root)
    p.init_from_preset(args.preset, name=args.name or root.name, force=args.force)
    res = render(p)
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.is_file() else []
    if ".factory/jobs/" not in lines:
        # .factory/.gitignore covers runtime state; this line also keeps jobs out of tools that ignore nested files.
        gi.write_text("\n".join([*lines, "", "# software factory runtime state", ".factory/jobs/"]).lstrip() + "\n")
    return {"root": str(root), "preset": args.preset, **res.to_json()}


def cmd_validate(args: argparse.Namespace) -> Any:
    loaded = _project(args).load()
    return {"ok": loaded.ok, "errors": loaded.errors, "warnings": loaded.warnings,
            "agents": sorted(loaded.agents), "skills": sorted(loaded.skills)}


def cmd_render(args: argparse.Namespace) -> Any:
    res = render(_project(args), check=args.check)
    if res.errors:
        raise FactoryError("config has errors:\n  " + "\n  ".join(res.errors))
    return res.to_json()


def doctor(project: Project, only: str | None = None) -> list[dict[str, Any]]:
    from ajantrik.core.connections import git_host
    from ajantrik.core.connections import tickets as tix

    out: list[dict[str, Any]] = []
    loaded = project.load()
    if not only:
        out.append({"name": "config", "ok": loaded.ok,
                    "detail": "; ".join(loaded.errors) or f"{len(loaded.agents)} agents, {len(loaded.skills)} skills"})
        if loaded.ok:
            drift = render(project, check=True)
            out.append({"name": "render", "ok": not drift.changed,
                        "detail": "up to date" if not drift.changed else
                        f"{len(drift.changed)} file(s) differ from the config; run `factory render`"})
    cfg = loaded.cfg
    if cfg is None:
        return out
    if only in (None, "git"):
        ok, detail = git_host.check(cfg.connections.git.model_dump(), project.root)
        out.append({"name": f"git ({cfg.connections.git.provider})", "ok": ok, "detail": detail})
    if only in (None, "tickets"):
        ok, detail = tix.check(cfg.connections.tickets.model_dump())
        out.append({"name": f"tickets ({cfg.connections.tickets.provider})", "ok": ok, "detail": detail})
    if only in (None, "runtime"):
        out.append(check_runtime(cfg))
    return out


def check_runtime(cfg: Any) -> dict[str, Any]:
    rt = cfg.runtime
    name = f"runtime ({rt.kind})"
    if rt.kind in ("claude-code", "claude-cli"):
        b = shutil.which(rt.claude_cli.binary)
        return {"name": name, "ok": bool(b), "detail": f"{rt.claude_cli.binary} at {b}" if b else
                f"{rt.claude_cli.binary} not found on PATH"}
    if rt.kind == "codex-cli":
        b = shutil.which(rt.codex_cli.binary)
        return {"name": name, "ok": bool(b), "detail": f"{rt.codex_cli.binary} at {b}" if b else
                f"{rt.codex_cli.binary} not found on PATH (npm i -g @openai/codex)"}
    from ajantrik.editions.headless.backends.llm_api import probe

    model = rt.models.get(rt.default_model, rt.default_model)
    ok, detail = asyncio.run(probe(rt.api.model_dump(), model))
    return {"name": name, "ok": ok, "detail": detail}


def _runner(args: argparse.Namespace) -> Any:
    from ajantrik.editions.headless.runner import Runner

    p = _project(args)
    res = render(p)  # the engine reads the rendered factory.json; keep it in step with the config
    if res.errors:
        raise FactoryError("config has errors:\n  " + "\n  ".join(res.errors))
    say = (lambda m: print(m, file=sys.stderr)) if args.json else print
    return Runner(p, say=say, wait_for_approval=getattr(args, "wait", False))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="factory", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", help="repo root (default: found from the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--preset", default="blank")
    i.add_argument("--name")
    i.add_argument("--force", action="store_true")
    sub.add_parser("presets")
    sub.add_parser("validate")
    r = sub.add_parser("render")
    r.add_argument("--check", action="store_true")
    s = sub.add_parser("studio")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--all", action="store_true", help="open the projects page even inside a repo")
    pr = sub.add_parser("projects")
    prs = pr.add_subparsers(dest="action")
    pa = prs.add_parser("add")
    pa.add_argument("path")
    pa.add_argument("--runtime", default="claude-code", choices=["claude-code", "claude-cli", "codex-cli", "api"])
    pa.add_argument("--preset", default="blank")
    pa.add_argument("--name")
    prm = prs.add_parser("remove")
    prm.add_argument("id")
    d = sub.add_parser("doctor")
    d.add_argument("--only", choices=["git", "tickets", "runtime"])
    run = sub.add_parser("run")
    run.add_argument("request", nargs="?", default="")
    run.add_argument("--ticket")
    run.add_argument("--base")
    run.add_argument("--wait", action="store_true", help="wait at approvals instead of stopping")
    for name in ("go", "resume"):
        g = sub.add_parser(name)
        g.add_argument("job")
        g.add_argument("--wait", action="store_true")
    nx = sub.add_parser("next", help="claim the first backlog item and run it (headless) or just create it")
    nx.add_argument("--wait", action="store_true")
    nx.add_argument("--create-only", action="store_true")
    nx.add_argument("--items", type=_indices, help="0-based backlog item indices, e.g. 0,2")
    return ap


def _indices(s: str) -> list[int]:
    parts = s.split(",")
    if not all(p.isascii() and p.isdigit() for p in parts):
        raise argparse.ArgumentTypeError(f"{s!r}: expected comma-separated item numbers like 0,2")
    return [int(p) for p in parts]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    root_arg: list[str] = []
    if "--root" in argv:
        i = argv.index("--root")
        root_arg = argv[i: i + 2]
        del argv[i: i + 2]
    cmd = next((a for a in argv if not a.startswith("-")), "")
    try:
        if cmd in JOB_COMMANDS or (cmd == "next" and "--create-only" in argv):
            root = Path(root_arg[1]).resolve() if root_arg else Project.find().root
            if not (root / ".factory" / "factory.json").is_file():
                raise FactoryError("run `factory render` first")
            argv = [a for a in argv if a != "--create-only"]
            return core_cli.main((["--json"] if as_json else []) + argv, root=root)
        args = build_parser().parse_args(root_arg + argv)
        args.json = as_json
        result = dispatch(args)
    except (FactoryError, FileNotFoundError, FileExistsError) as e:
        print(f"factory: {e}", file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, indent=2) if as_json or not isinstance(result, str) else result)
    return 0


def dispatch(args: argparse.Namespace) -> Any:
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "presets":
        return list_presets()
    if args.cmd == "validate":
        res = cmd_validate(args)
        if not res["ok"]:
            raise FactoryError("config has errors:\n  " + "\n  ".join(res["errors"]))
        return res
    if args.cmd == "render":
        return cmd_render(args)
    if args.cmd == "studio":
        from ajantrik.studio.server import serve

        try:
            p: Project | None = None if args.all else _project(args)
        except FileNotFoundError:
            p = None  # not inside a configured repo: the projects page
        loaded = p.load() if p else None
        port = args.port or (loaded.cfg.ui.studio_port if loaded and loaded.cfg else 8765)
        serve(p.root if p else None, port, open_browser=not args.no_browser)
        return None
    if args.cmd == "projects":
        from ajantrik.kit import workspace

        if args.action == "add":
            res = workspace.create(args.path, args.runtime, args.preset, args.name)
            return res if args.json else (f"{'set up and added' if res['set_up'] else 'added'} {res['project']['id']} "
                                          f"({res['project']['root']}), {res['written']} files rendered")
        if args.action == "remove":
            workspace.Registry().remove(args.id)
            return {"removed": args.id} if args.json else f"forgot {args.id} (its files are untouched)"
        rows = [workspace.summary(e) for e in workspace.Registry().all()]
        if args.json:
            return rows
        if not rows:
            return "no projects yet: factory projects add <repo path>"
        return "\n".join(f"{r['id']:<20} {r.get('runtime', '?'):<12} {len(r.get('jobs', {}).get('active', [])):>2} active  "
                         f"{r.get('backlog', {}).get('queued', 0):>2} queued  {r['root']}" for r in rows)
    if args.cmd == "doctor":
        checks = doctor(_project(args), args.only)
        if not args.json:
            return "\n".join(f"{'ok ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}" for c in checks)
        return checks
    if args.cmd == "run":
        job = _runner(args).run(args.request, args.ticket, args.base)
        return job if args.json else None
    if args.cmd == "go":
        job = _runner(args).go(args.job, os.environ.get("FACTORY_APPROVER") or os.environ.get("USER", "human"))
        return job if args.json else None
    if args.cmd == "resume":
        job = _runner(args).resume(args.job)
        return job if args.json else None
    if args.cmd == "next":
        from ajantrik.core.connections import tickets as tix

        runner = _runner(args)
        if args.items is not None:
            conn = (runner.engine.cfg.get("connections") or {}).get("tickets") or {}
            kp = conn.get("key_pattern") or tix.KEY_PATTERN
            claim = tix.claim_items(runner.project.root, args.items, kp)
            try:
                job = runner.engine.new_job(claim["request"], claim["key"], claim["base"])
            except Exception:
                tix.release_items(runner.project.root, claim["items"])
                raise
            for it in claim["items"]:
                tix.mark_claimed(runner.project.root, it["claimed"], job["id"])
            job = asyncio.run(runner.drive(job["id"]))
            return job if args.json else None
        item = tix.claim_next(runner.project.root, runner.engine.cfg)
        if not item:
            return {"claimed": None, "note": "nothing queued"}
        job = runner.engine.new_job(item["text"], item["key"], item["base"])
        tix.mark_claimed(runner.project.root, item["line"], job["id"])
        job = asyncio.run(runner.drive(job["id"]))
        return job if args.json else None
    raise FactoryError(f"unknown command {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
