"""`python3 .factory/factory.py <command>`: the factory's stdlib-only command line inside a repo.

The interactive Claude Code orchestrator (/factory) drives jobs with these; humans can too. The
installed `factory` CLI (ajantrik.kit.cli) offers the same job commands plus init, render, studio and
the headless runtimes.

    new "<request>" | --ticket KEY [--base B] [--title T --type Bug --epic E]   create a job
    step <job>        print the next action as JSON (agents to run, gate, approval, deliver, stop)
    advance <job>     after a stage's agents finished: check their files and move the job on
    refresh <job>     copy reviewer verdicts that have landed into job.json
    gate <job>        run the gate for the current round and move the job on
    approve <job> [--stage S]       record a human approval (the user's command, never an agent's)
    rework <job> "<note>" [--scope] send the job back with a note
    deliver <job>     push / open the PR / put commits on the base, as configured
    abandon <job> [--force]
    status            one line per job, then the backlog
    sync [--push]     fill .factory/backlog.md from the ticket system; --push first creates issues
                      for queued items without a ticket (a human's command, never an agent's)
    next              claim the first backlog item and create its job
    route <job>       which reviewers this diff needs, and why
    ticket <KEY>      read a ticket (REST)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import jobs
from .connections import tickets as tix
from .engine import Engine
from .routing import added_lines, changed_paths, route_reviewers
from .util import FactoryError, read_json


def build_parser(prog: str = "factory.py") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description="Software factory job commands.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="create a job")
    n.add_argument("request", nargs="?", default="")
    n.add_argument("--ticket")
    n.add_argument("--base")
    n.add_argument("--title", help="ticket summary (when the session read the ticket itself, e.g. through MCP)")
    n.add_argument("--type", dest="ticket_type")
    n.add_argument("--epic")
    n.add_argument("--skip-preflight", action="store_true")
    for name in ("step", "advance", "refresh", "gate", "route"):
        p = sub.add_parser(name)
        p.add_argument("job")
    a = sub.add_parser("approve")
    a.add_argument("job")
    a.add_argument("--stage")
    r = sub.add_parser("rework")
    r.add_argument("job")
    r.add_argument("note")
    r.add_argument("--scope", action="store_true", help="the note changes scope: re-run the spec too")
    d = sub.add_parser("deliver")
    d.add_argument("job")
    ab = sub.add_parser("abandon")
    ab.add_argument("job")
    ab.add_argument("--force", action="store_true")
    sub.add_parser("status")
    sy = sub.add_parser("sync")
    sy.add_argument("--push", action="store_true", help="also create Jira issues for queued items without a ticket")
    nx = sub.add_parser("next")
    nx.add_argument("--skip-preflight", action="store_true")
    t = sub.add_parser("ticket")
    t.add_argument("key")
    g = sub.add_parser("gate-run", help="run the gate on any worktree, outside a job (what gate.sh calls)")
    g.add_argument("worktree")
    g.add_argument("base")
    g.add_argument("out")
    return ap


def approver() -> str:
    return os.environ.get("FACTORY_APPROVER") or getpass.getuser()


def run_command(args: argparse.Namespace, root: Path) -> Any:
    eng = Engine(root)
    cmd = args.cmd
    if cmd == "new":
        return eng.new_job(args.request, args.ticket, args.base, args.title, args.ticket_type, args.epic,
                           args.skip_preflight)
    if cmd == "step":
        return eng.step(args.job)
    if cmd == "advance":
        return eng.advance(args.job)
    if cmd == "refresh":
        return eng.refresh(args.job)
    if cmd == "gate":
        return eng.run_gate(args.job)
    if cmd == "approve":
        return eng.approve(args.job, approver(), args.stage)
    if cmd == "rework":
        return eng.rework(args.job, args.note, args.scope)
    if cmd == "deliver":
        return eng.deliver(args.job, approver())
    if cmd == "abandon":
        return eng.abandon(args.job, args.force)
    if cmd == "status":
        return status(root)
    if cmd == "sync":
        return {"report": tix.sync(root, eng.cfg, jobs.list_jobs(root), push=args.push)}
    if cmd == "next":
        item = tix.claim_next(root, eng.cfg)
        if not item:
            return {"claimed": None, "note": "nothing queued"}
        job = eng.new_job(item["text"], item["key"], item["base"], skip_preflight=args.skip_preflight)
        tix.mark_claimed(root, item["line"], job["id"])
        return job
    if cmd == "route":
        job = eng.get(args.job)
        wt = Path(job["worktree"])
        gate = read_json(jobs.job_dir(root, job["id"]) / f"round-{job['round']}" / "gate.json", {})
        return route_reviewers(eng.cfg, changed_paths(wt, job["base"]), added_lines(wt, job["base"]), gate)
    if cmd == "gate-run":
        from .gate import run_gate

        ok, result = run_gate(root, eng.cfg, Path(args.worktree).resolve(), args.base, Path(args.out).resolve())
        if not ok:
            raise FactoryError(f"gate failed: {json.dumps(result)}")
        return result
    if cmd == "ticket":
        return tix.Jira(eng.cfg["connections"]["tickets"]).get(args.key)
    raise FactoryError(f"unknown command {cmd}")


def status(root: Path) -> dict[str, Any]:
    js = jobs.list_jobs(root)
    backlog = tix.backlog_path(root)
    text = backlog.read_text() if backlog.is_file() else ""
    return {"jobs": [{k: j.get(k) for k in ("id", "stage", "round", "agent", "note", "branch", "updated")} for j in js],
            "backlog": {"queued": text.count("\n- [ ] ") + text.startswith("- [ ] "),
                        "blocked": text.count("\n- [!] ") + text.startswith("- [!] ")}}


def human(result: Any) -> str:
    if isinstance(result, dict) and "jobs" in result and "backlog" in result:
        lines = [f"{j['id']}  {j['stage']}  round {j['round']}  {j['agent'] or j['note'] or ''}" for j in result["jobs"]]
        b = result["backlog"]
        return "\n".join([*lines, f"backlog: {b['queued']} queued, {b['blocked']} blocked"])
    if isinstance(result, dict) and "id" in result and "stage" in result:
        return f"{result['id']}: {result['stage']}" + (f" ({result['note']})" if result.get("note") else "")
    return json.dumps(result, indent=2)


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = root or Path(os.environ.get("FACTORY_ROOT") or Path.cwd())
    try:
        result = run_command(args, root.resolve())
    except FactoryError as e:
        print(f"factory: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if args.json or args.cmd in ("step", "route", "ticket") else human(result))
    return 0
