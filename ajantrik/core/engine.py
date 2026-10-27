"""The loop, as data. Every runtime drives a job through the same three calls:

    step(job)     what to do next: run agents, run the gate, wait for an approval, deliver, or stop
    advance(job)  after the agents of a stage finished: check what they wrote and move the job on
    gate(job) / approve(job) / deliver(job) / rework(job) for the non-agent steps

The interactive Claude Code orchestrator calls these through `.factory/factory.py` and spawns the
agents `step` names; the headless runner calls them in-process and runs the agents through its
backend. Either way job.json, the round folders and the marker lines are the same, so a job can
start in one runtime and finish in another.

Stages are agents, approvals and gates. An agent's phase says what its output means (graph.role_of):
plan writes a file, build is a builder (build-<id>.md, rebuilt with the findings of a failed round),
review a reviewer (a VERDICT each round) and integrate the integrator (READY or ESCALATE). A stage
whose success edges fan out to several agents starts a group: they run at the same time, the job's
stage is the first of them, and the path joins again at the stage they all lead to.

Job stages: a stage id (a group's first) while it runs, `awaiting-<id>` while an approval waits for a
human, `deliver` / `awaiting-deliver` for the delivery that follows the last stage, then `ready`,
`delivered`, `needs-human` or `abandoned`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import delivery as deliv
from . import gate as gate_mod
from . import jobs, sandbox
from .connections import tickets as tix
from .graph import DELIVER, groups, role_of, run_order, successor
from .routing import added_lines, changed_paths, refused, route_reviewers
from .util import (
    FactoryError,
    first_line,
    git,
    now,
    read_json,
    ref_exists,
    run,
    section_first_line,
    slug,
    underscore_words,
)

TERMINAL = {"ready", "delivered", "needs-human", "abandoned"}


def role(stage: dict[str, Any]) -> str:
    """An agent stage's role from its phase ("" for approvals and gates)."""
    return role_of(stage)


def load_config(root: Path) -> dict[str, Any]:
    cfg = read_json(root / ".factory" / "factory.json")
    if not isinstance(cfg, dict):
        raise FactoryError(f"{root}/.factory/factory.json missing or unreadable; run `factory render`")
    return cfg


class Engine:
    def __init__(self, root: Path, cfg: dict[str, Any] | None = None):
        self.root = root.resolve()
        self.cfg = cfg or load_config(self.root)
        self.stages: list[dict[str, Any]] = self.cfg["loop"]["stages"]
        # The run order follows the success edges from the start (see graph.py), not the list.
        self.order: list[str] = run_order(self.stages, self.cfg["loop"].get("start"))
        self.groups: dict[str, list[str]] = {
            sid: g for g in groups(self.stages, self.cfg["loop"].get("start")) for sid in g}
        self.path: list[dict[str, Any]] = [s for sid in self.order for s in self.stages if s["id"] == sid]
        self.max_rounds: int = self.cfg["loop"].get("max_rounds", 3)
        self.delivery: dict[str, Any] = {"id": DELIVER, "kind": "delivery", "title": "Deliver"}

    # ---- lookups ------------------------------------------------------------------------------------
    def stage(self, sid: str) -> dict[str, Any] | None:
        if sid == DELIVER:
            return self.delivery
        return next((s for s in self.stages if s["id"] == sid), None)

    def of_role(self, r: str) -> list[dict[str, Any]]:
        """Agent stages on the run path with this role, in order."""
        return [s for s in self.path if role(s) == r]

    def group(self, sid: str) -> list[dict[str, Any]]:
        """The stages that run together with sid: the branches of the fan-out it is in (see graph.groups)."""
        st = self.stage(sid)
        if st is None:
            return []
        return [s for g in self.groups.get(sid, [sid]) if (s := self.stage(g))]

    def stage_of_kind(self, kind: str) -> dict[str, Any] | None:
        return next((s for s in self.path if s["kind"] == kind), None)

    def index(self, sid: str) -> int:
        """Position in the run order."""
        return self.order.index(sid)

    def current(self, job: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """(stage config, waiting for approval?) for the job's stage value."""
        st = job["stage"]
        if st.startswith("awaiting-"):
            return self.stage(st.removeprefix("awaiting-")), True
        return self.stage(st), False

    def dir(self, job: dict[str, Any]) -> Path:
        return jobs.job_dir(self.root, job["id"])

    def worktree_root(self) -> Path:
        repo = self.cfg["repo"]
        return Path(os.path.normpath(self.root / repo["worktree_root"].replace("{repo}", repo["name"])))

    def get(self, job_ref: str) -> dict[str, Any]:
        return jobs.read(self.root, jobs.resolve_id(self.root, job_ref))

    def save(self, job: dict[str, Any], **fields: Any) -> dict[str, Any]:
        return jobs.update(self.root, job, **fields)

    # ---- creating a job -----------------------------------------------------------------------------
    def new_job(self, request: str = "", ticket: str | None = None, base: str | None = None,
                title: str | None = None, ticket_type: str | None = None, epic: str | None = None,
                skip_preflight: bool = False) -> dict[str, Any]:
        conn = self.cfg["connections"]["tickets"]
        default_base = self.cfg["repo"]["default_base"]
        note = ""
        if ticket and conn.get("provider") == "jira" and conn.get("mode") == "rest" and not title:
            issue = tix.Jira(conn).get(ticket)
            title, ticket_type, epic = issue["summary"], issue["type"], issue["epic"]
        feature = (title or request).strip()
        if not feature:
            raise FactoryError("a job needs a request (free text) or a ticket")
        if not base:
            if ticket:
                remote = self.cfg["connections"]["git"].get("remote", "origin")
                resolved, reason = tix.resolve_epic_base(self.root, conn, epic, default_base, remote)
                if not resolved:
                    raise FactoryError(f"cannot pick a base branch: {reason} (pass --base)")
                base = resolved
            else:
                base = default_base
        if not ticket:
            note = "no ticket"
        if not skip_preflight:
            self.preflight(base)

        job_id = jobs.allocate(self.root, (f"{ticket}-" if ticket else "") + slug(feature))
        num = job_id.split("-", 1)[0]
        conv = self.cfg["conventions"]
        types = conv.get("branch_types") or {}
        btype = types.get(ticket_type or "", types.get("default", "feature"))
        pattern = conv["branch"] if ticket else conv["branch_no_ticket"]
        branch = pattern.format(type=btype, key=ticket or "", num=num, words=underscore_words(feature))
        worktree = self.worktree_root() / job_id
        try:
            if ref_exists(self.root, f"refs/heads/{branch}"):
                raise FactoryError(f"branch {branch} already exists")
            start = base if ref_exists(self.root, base) else f"origin/{base}"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "-C", str(self.root), "worktree", "add", "-b", branch, str(worktree), start])
            self.setup_worktree(worktree)
        except Exception:
            shutil.rmtree(jobs.job_dir(self.root, job_id), ignore_errors=True)
            raise
        first = self.path[0]
        job: dict[str, Any] = {
            "id": job_id, "ticket": ticket, "jira": ticket if conn.get("provider") == "jira" else None,
            "ticket_type": ticket_type or "", "feature": feature, "request": request or feature, "base": base,
            "branch": branch, "worktree": str(worktree), "stage": "", "round": 0, "round_base": 0,
            "agent": "", "builders": [], "rework": 0,
            # Round 1 opens at the first check after a build; checks before any builder run in round 0.
            "round_due": not self.of_role("builder"),
            "gate": self._pending_gate(), "reviews": self._pending_reviews(),
            "started": now(), "stageSince": now(), "note": note, "runtime": self.cfg["runtime"]["kind"],
        }
        jobs.write(self.root, job)
        return self.enter(job, first["id"])

    def preflight(self, base: str) -> None:
        remote = self.cfg["connections"]["git"].get("remote", "origin")
        if not ref_exists(self.root, base):
            run(["git", "-C", str(self.root), "fetch", remote, base], check=False, timeout=120)
            if not ref_exists(self.root, f"{remote}/{base}"):
                raise FactoryError(f"base branch {base} exists neither locally nor on {remote}")
        env = {**os.environ, "BASE": base, "REPO": str(self.root), "FACTORY_DIR": str(self.root / ".factory")}
        for p in self.cfg.get("preflight") or []:
            r = subprocess.run(["bash", "-c", p["cmd"]], cwd=self.root, capture_output=True, text=True, env=env)
            if r.returncode != 0:
                raise FactoryError(f"preflight failed: {p['cmd']}" + (f". {p['hint']}" if p.get("hint") else ""))

    def setup_worktree(self, wt: Path) -> None:
        for step in self.cfg.get("worktree") or []:
            if step["kind"] in ("symlink", "copy"):
                src, dst = self.root / step["path"], wt / step["path"]
                if not src.exists():
                    continue
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                elif dst.is_dir():
                    shutil.rmtree(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if step["kind"] == "symlink":
                    dst.symlink_to(src)
                elif src.is_dir():
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)
            else:
                env = {**os.environ, "WORKTREE": str(wt), "REPO": str(self.root),
                       "FACTORY_DIR": str(self.root / ".factory")}
                # Setup installs dependencies, whose install scripts are code too: it runs in the sandbox.
                sb = self.cfg.get("sandbox") or {}
                jd = jobs.job_dir(self.root, wt.name)
                try:
                    argv = sandbox.wrap(["bash", "-c", step["cmd"]], sb if (sb.get("apply") or {}).get("gate", True)
                                        else None, sandbox.settings_path(self.root, f"setup-{wt.name}"),
                                        sandbox.writable_for(wt, jd))
                except sandbox.SandboxUnavailable as e:
                    raise FactoryError(f"worktree setup `{step['cmd']}`: {e}") from None
                r = subprocess.run(argv, cwd=wt, capture_output=True, text=True, env=env)
                if r.returncode != 0:
                    raise FactoryError(f"worktree setup `{step['cmd']}` failed: {r.stderr.strip()[-300:]}")

    def _pending_gate(self) -> dict[str, Any]:
        steps = [s["key"] for s in self.cfg["gate"].get("steps") or []]
        gates = [s for s in self.path if s["kind"] == "gate"]
        keys = {k for g in gates for k in (g.get("steps") or [*steps, "secrets"])}
        g: dict[str, Any] = {k: "pending" for k in steps if k in keys}
        if "secrets" in keys and self.cfg["gate"].get("secrets", True):
            g["secrets"] = "pending"
        return g

    def _pending_reviews(self) -> dict[str, str]:
        return {s["id"]: "pending" for s in self.of_role("reviewer")}

    # ---- moving between stages ----------------------------------------------------------------------
    def enter(self, job: dict[str, Any], sid: str | None) -> dict[str, Any]:
        """Put the job into stage sid (None: past the last stage). A stage in a parallel group enters the group."""
        if sid is None:
            return self.save(job, stage="ready", agent="", note=self._ready_note(job))
        st = self.stage(sid)
        assert st is not None
        group = self.group(sid)
        sid = group[0]["id"]
        kind = st["kind"]
        if kind == "approval":
            return self.save(job, stage=f"awaiting-{sid}", agent="",
                             note=st.get("note") or f"approve to continue ({sid})")
        if kind == "delivery":
            mode = self.cfg["delivery"]["mode"]
            if mode == "none":
                return self.save(job, stage="ready", agent="", note=self._ready_note(job))
            if self.cfg["delivery"].get("require_approval", True):
                return self.save(job, stage=f"awaiting-{sid}", agent="", note=f"approve delivery ({mode})")
            return self.save(job, stage=sid, agent="", note="")
        if kind == "gate" or any(role(s) == "reviewer" for s in group):
            # The first check after something was built (or sent back) opens a round.
            fields: dict[str, Any] = {}
            if job.get("round_due", True):
                rnd = job["round"] + 1
                (self.dir(job) / f"round-{rnd}").mkdir(parents=True, exist_ok=True)
                fields = {"round": rnd, "reviews": self._pending_reviews(), "gate": self._pending_gate(),
                          "round_due": False, "rebuild": None}
            return self.save(job, stage=sid, agent="gate" if kind == "gate" else "", note="", **fields)
        # The note is for a human looking at a waiting or stopped job; a running stage clears it.
        return self.save(job, stage=sid, agent="", note="" if job.get("note") != "no ticket" else job["note"])

    def next_stage_id(self, sid: str) -> str | None:
        if sid == DELIVER:
            return None
        nxt = successor(self.stages, sid)
        if nxt is None and self.cfg["delivery"]["mode"] != "none":
            return DELIVER
        return nxt

    def needs_human(self, job: dict[str, Any], note: str) -> dict[str, Any]:
        return self.save(job, stage="needs-human", agent="", note=note[:300])

    def _ready_note(self, job: dict[str, Any]) -> str:
        return "ready: push and open the PR" if self.cfg["delivery"]["mode"] == "none" else "ready"

    def rounds_used(self, job: dict[str, Any]) -> int:
        return job["round"] - job.get("round_base", 0)

    # ---- prompts ------------------------------------------------------------------------------------
    def preamble(self, job: dict[str, Any]) -> str:
        return (f"Job folder: {self.dir(job)}/. Worktree: {job['worktree']}. Branch: {job['branch']}. "
                f"Base: {job['base']}. Repo: {self.root}. Ticket: {job.get('ticket') or 'none'}. "
                f"Round: {job['round']}. Commit format: {self.cfg['conventions']['commit']}.")

    def _builder_line(self) -> str:
        return "; ".join(f"{b['id']} ({b['agent']}): {b.get('description') or ', '.join(b.get('area_globs') or [])}"
                         for b in self.of_role("builder"))

    def _rework_line(self, job: dict[str, Any]) -> str:
        k = job.get("rework") or 0
        return f" Rework: read rework-{k}.md and address it." if k else ""

    def agent_prompt(self, job: dict[str, Any], st: dict[str, Any]) -> str:
        parts = [self.preamble(job)]
        if st.get("read_builders"):
            parts.append(f"Builders available: {self._builder_line()}. Write a line `Builders: <id>, <id>` in "
                         f"{st['output']} naming the builders this job needs.")
        if role(st) == "integrator":
            t = self.cfg["connections"]["tickets"]
            wb = "draft" if t.get("provider") == "jira" and t.get("write_back") != "none" else "none"
            parts.append(f"Final round: {job['round']}. PR template: {self.cfg['conventions'].get('pr_template') or 'none'}. "
                         f"Ticket write-back: {wb}.")
        if st["id"] == self.path[0]["id"] or st.get("agent") == "spec-writer":
            parts.append(f"Feature: {job.get('request') or job['feature']}")
        parts.append(f"Write {st['output']} in the job folder.")
        if st.get("prompt"):
            parts.append(st["prompt"])
        return " ".join(parts) + self._rework_line(job)

    def builder_prompt(self, job: dict[str, Any], b: dict[str, Any], resume: bool) -> dict[str, str]:
        others = [x for x in job["builders"] if x != b["id"]]
        area = ", ".join(b.get("area_globs") or []) or "everything the design assigns to you"
        head = (f"{self.preamble(job)} You are builder {b['id']}. Write build-{b['id']}.md. Your area: {area}. "
                f"Other builders: {', '.join(others) or 'none'}.")
        if b.get("prompt"):
            head = f"{head} {b['prompt']}"
        n = job["round"]
        if job.get("rework") and not resume:
            fresh = f"{head} Rework: read rework-{job['rework']}.md and every round folder, address it, then update " \
                    f"build-{b['id']}.md for round {n + 1}."
            return {"prompt": fresh, "resume": ""}
        if n == 0:
            return {"prompt": f"{head} Build round 1.", "resume": ""}
        findings = (f"Round {n} findings are in {self.dir(job)}/round-{n}/ (gate.log, gate.json and every "
                    f"review-*.md with VERDICT: CHANGES). Address the ones in your area, commit, and update "
                    f"build-{b['id']}.md for round {n + 1}.")
        return {"prompt": f"{head} Build round {n + 1}. Read every round folder and build file first. {findings}",
                "resume": findings}

    # ---- step ---------------------------------------------------------------------------------------
    def step(self, job_ref: str) -> dict[str, Any]:
        job = self.get(job_ref)
        base = {"job": job["id"], "stage": job["stage"]}
        if job["stage"] in TERMINAL:
            out = {**base, "type": "stop", "note": job.get("note", "")}
            if job["stage"] == "ready" and self.cfg["delivery"]["mode"] == "none":
                out["commands"] = deliv.human_commands(self.cfg, job, self.dir(job))
            return out
        st, waiting = self.current(job)
        if st is None:
            return {**base, "type": "stop", "note": f"unknown stage {job['stage']!r} for this loop"}
        if waiting:
            if jobs.approved(self.root, job, st["id"]):
                job = self._after(job, st)
                return self.step(job["id"])
            kind = "delivery-approval" if st["kind"] == "delivery" else "approval"
            return {**base, "type": "approval", "kind": kind, "approval_stage": st["id"], "note": job["note"]}
        kind = st["kind"]
        if kind == "agent":
            return self._agents_step(job, self.group(st["id"]), base)
        if kind == "gate":
            return {**base, "type": "gate", "round": job["round"]}
        if kind == "delivery":
            return {**base, "type": "deliver", "mode": self.cfg["delivery"]["mode"]}
        return {**base, "type": "stop", "note": f"stage kind {kind} has no action"}

    def _build_targets(self, job: dict[str, Any]) -> list[str]:
        """The builders that run now: the ones the design picked, narrowed to the owners of the findings."""
        return job.get("rebuild") or job["builders"] or [b["id"] for b in self.of_role("builder")]

    def _agents_step(self, job: dict[str, Any], group: list[dict[str, Any]], base: dict[str, Any]) -> dict[str, Any]:
        """One spec per agent in the group; reviewers routed out and builders not targeted are left out."""
        if not job["builders"] and any(role(s) == "builder" for s in group):
            job = self.save(job, builders=[b["id"] for b in self.of_role("builder")])
        targets = self._build_targets(job)
        resume = job["round"] > 0 and not job.get("fresh_builders")
        rdir = self.dir(job) / f"round-{job['round']}"
        reviews = dict(job.get("reviews") or {})
        routes: dict[str, str] | None = None
        specs: list[dict[str, Any]] = []
        for s in group:
            r = role(s)
            if r == "builder":
                if s["id"] not in targets:
                    continue
                p = self.builder_prompt(job, s, resume)
                specs.append({"agent": s["agent"], "name": f"builder-{s['id']}-{job['id']}", "prompt": p["prompt"],
                              "resume": resume, "resume_message": p["resume"], "expect": f"build-{s['id']}.md"})
            elif r == "reviewer":
                if routes is None:
                    wt = Path(job["worktree"])
                    rdir.mkdir(parents=True, exist_ok=True)
                    gate = read_json(rdir / "gate.json", {}) or {}
                    routes = route_reviewers(self.cfg, changed_paths(wt, job["base"]), added_lines(wt, job["base"]),
                                             gate, [x for x in group if role(x) == "reviewer"])
                reason = routes.get(s["id"], "")
                if not reason:
                    reviews[s["id"]] = "skipped"
                    continue
                verdict = _verdict(rdir / f"review-{s['id']}.md")
                reviews[s["id"]] = verdict or "pending"
                if verdict:
                    continue  # resume: this reviewer already wrote its file
                specs.append({"agent": s["agent"], "name": f"{s['agent']}-{job['id']}-r{job['round']}",
                              "reason": reason,
                              "prompt": f"{self.preamble(job)} You are reviewer {s['id']} (routed: {reason}). Review "
                                        f"round {job['round']}. Write round-{job['round']}/review-{s['id']}.md."
                                        + (f" {s['prompt']}" if s.get("prompt") else ""),
                              "expect": f"round-{job['round']}/review-{s['id']}.md"})
            else:
                if r == "integrator":
                    wt = Path(job["worktree"])
                    why = refused(self.cfg, changed_paths(wt, job["base"]), added_lines(wt, job["base"]))
                    if why:
                        self.needs_human(job, f"{why}: needs a human security review")
                        return self.step(job["id"])
                specs.append({"agent": s["agent"], "name": f"{s['agent']}-{job['id']}",
                              "prompt": self.agent_prompt(job, s), "expect": s["output"]})
        self.save(job, reviews=reviews, agent=", ".join(x["agent"] for x in specs))
        if not specs:
            return {**base, "type": "advance", "note": "nothing left to run in this stage"}
        return {**base, "type": "agents", "parallel": len(specs) > 1, "resume": any(x.get("resume") for x in specs),
                "refresh": any(role(s) == "reviewer" for s in group), "agents": specs}

    # ---- advance ------------------------------------------------------------------------------------
    def refresh(self, job_ref: str) -> dict[str, Any]:
        """Copy reviewer verdicts that have landed into job.json, so dashboard chips fill in live."""
        job = self.get(job_ref)
        st, waiting = self.current(job)
        if st and not waiting and any(role(s) == "reviewer" for s in self.group(st["id"])):
            rdir = self.dir(job) / f"round-{job['round']}"
            reviews = dict(job["reviews"])
            for rid, v in reviews.items():
                if v == "pending":
                    reviews[rid] = _verdict(rdir / f"review-{rid}.md") or "pending"
            job = self.save(job, reviews=reviews)
        return job

    def advance(self, job_ref: str) -> dict[str, Any]:
        job = self.get(job_ref)
        st, waiting = self.current(job)
        if st is None or waiting or job["stage"] in TERMINAL or st["kind"] != "agent":
            return job
        d = self.dir(job)
        group = self.group(st["id"])
        targets = self._build_targets(job)
        reviews = dict(job["reviews"])
        built = False
        changes: list[str] = []
        for s in group:
            r = role(s)
            if r == "builder":
                if s["id"] not in targets:
                    continue
                f = d / f"build-{s['id']}.md"
                line = first_line(f)
                if not f.is_file():
                    return self.needs_human(job, f"builder {s['id']} finished without writing {f.name}")
                if "BLOCKED" in line.upper():
                    return self.needs_human(job, f"builder {s['id']}: " + (section_first_line(f, "Blocked") or line))
                if "READY" not in line.upper():
                    return self.needs_human(job, f"{f.name} first line is not STATUS: READY|BLOCKED: {line}")
                built = True
            elif r == "reviewer":
                if reviews.get(s["id"]) in ("skipped", "not-run"):
                    continue
                verdict = _verdict(d / f"round-{job['round']}" / f"review-{s['id']}.md")
                if not verdict:
                    return self.needs_human(job, f"reviewer {s['id']} wrote no VERDICT line in round-{job['round']}")
                reviews[s["id"]] = verdict
                if verdict == "CHANGES":
                    changes.append(s["id"])
            elif r == "integrator":
                out = d / s["output"]
                decision = first_line(out).upper()
                if decision.startswith("ESCALATE"):
                    return self.needs_human(job, section_first_line(out, "For the human") or "integrator escalated")
                if not decision.startswith("READY"):
                    return self.needs_human(job, f"{s['output']} first line is not READY or ESCALATE")
                files = deliv.outgoing(d, self.cfg["security"].get("outgoing_files") or [])
                if files:
                    scan = self.root / ".factory" / "security" / "secret_scan.sh"
                    argv = [str(scan), str(d / "secrets-outgoing.json")]
                    for f in files:
                        argv += ["--file", str(f)]
                    if run(argv, check=False).returncode != 0:
                        return self.needs_human(job, "secret in the outgoing PR/ticket text: see secrets-outgoing.json")
            else:
                out = d / s["output"]
                if not out.is_file():
                    return self.needs_human(job, f"{s['agent']} finished without writing {s['output']}")
                if first_line(out).upper().startswith("BLOCKED"):
                    return self.needs_human(job, f"{s['output']}: {first_line(out)}")
                if s.get("read_builders"):
                    m = re.search(r"(?im)^\**builders:?\**\s*:?\s*(.+)$", out.read_text(errors="replace"))
                    known = [b["id"] for b in self.of_role("builder")]
                    picked = [x for x in re.findall(r"[a-z0-9_-]+", m.group(1).lower())] if m else []
                    picked = [x for x in picked if x in known] or known
                    job = self.save(job, builders=picked)
        fields: dict[str, Any] = {"reviews": reviews}
        if built:
            fields.update(fresh_builders=False, round_due=True)
        job = self.save(job, **fields)
        if changes:
            return self.next_round(job, self.stage(changes[0]) or st, f"reviewers {', '.join(changes)}", changes)
        return self._after(job, st)

    def _after(self, job: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        return self.enter(job, self.next_stage_id(self.group(st["id"])[-1]["id"]))

    def builder_before(self, sid: str) -> dict[str, Any] | None:
        """The nearest builder before sid on the run path: where findings go back to by default."""
        if sid not in self.order:
            return None
        return next((s for s in reversed(self.path[: self.index(sid)]) if role(s) == "builder"), None)

    def next_round(self, job: dict[str, Any], st: dict[str, Any], outstanding: str,
                   reviewers: list[str] | None = None, gate_steps: list[str] | None = None) -> dict[str, Any]:
        if self.rounds_used(job) >= self.max_rounds:
            (self.dir(job) / "decision.md").write_text(
                f"ESCALATE\n\nReview rounds exhausted. Outstanding in round-{job['round']}/: {outstanding}.\n\n"
                f"## For the human\n\n{self.max_rounds} rounds used; outstanding: {outstanding}.\n")
            return self.needs_human(job, f"{self.max_rounds} rounds used; outstanding: {outstanding}")
        target = st.get("on_fail") or (self.builder_before(st["id"]) or {}).get("id")
        if not target:
            return self.needs_human(job, f"{outstanding}; no builder before {st['id']} to send it back to")
        picked = set(job["builders"] or [b["id"] for b in self.of_role("builder")])
        rebuild = None
        if reviewers:
            owners = [(self.stage(r) or {}).get("builder") for r in reviewers]
            # Reviewers without an owner: findings could be anywhere in the diff.
            if all(owners):
                rebuild = sorted({str(o) for o in owners} & picked) or None
        if gate_steps:
            steps = {s["key"]: s for s in self.cfg["gate"]["steps"]}
            owners = [steps[k].get("builder") if k in steps else None for k in gate_steps]
            if all(owners):
                rebuild = sorted({str(o) for o in owners} & picked) or None
        job = self.save(job, rebuild=rebuild, round_due=True)
        return self.enter(job, target)

    # ---- gate ---------------------------------------------------------------------------------------
    def run_gate(self, job_ref: str) -> dict[str, Any]:
        job = self.get(job_ref)
        st, _ = self.current(job)
        if not st or st["kind"] != "gate":
            raise FactoryError(f"job {job['id']} is in {job['stage']}, not at a gate")
        out = self.dir(job) / f"round-{job['round']}"
        ok, result = gate_mod.run_gate(self.root, self.cfg, Path(job["worktree"]), job["base"], out,
                                       keys=st.get("steps") or None)
        gate_view = {k: v for k, v in result.items() if k in job["gate"] or k in ("coverage", "passed", "failed")}
        job = self.save(job, gate={**job["gate"], **gate_view}, agent="")
        if ok:
            return self._after(job, st)
        # Reviewers that have not run this round will not: the round ends here.
        reviews = {k: "not-run" if v == "pending" else v for k, v in job["reviews"].items()}
        job = self.save(job, reviews=reviews)
        if result.get("error"):
            return self.needs_human(job, f"gate setup: {result['error']}")
        ran = [k for k in st.get("steps") or [s["key"] for s in self.cfg["gate"]["steps"]]
               if k not in ("secrets", "clean-tree")]
        if result.get("external_only"):
            failing = [k for k in ran if result.get(k) == "FAIL"]
            return self.needs_human(job, f"only {', '.join(failing)} failed, which can fail for reasons outside the "
                                         "change; check round-*/gate.log, then rework to retry")
        failing = [k for k in [*ran, "secrets"] if result.get(k) == "FAIL"]
        shown = failing + (["dirty tree"] if result.get("dirty") else [])
        owned = failing if not result.get("dirty") and "secrets" not in failing else None
        return self.next_round(job, st, f"gate ({', '.join(shown)})", gate_steps=owned)

    # ---- approvals, rework, delivery, abandon -------------------------------------------------------
    def approve(self, job_ref: str, by: str, stage: str | None = None) -> dict[str, Any]:
        job = self.get(job_ref)
        st, waiting = self.current(job)
        if not waiting or st is None:
            raise FactoryError(f"job {job['id']} is in {job['stage']}; nothing waits for an approval")
        if stage and stage != st["id"]:
            raise FactoryError(f"job {job['id']} waits for {st['id']}, not {stage}")
        jobs.record_approval(self.root, job, st["id"], by)
        return self._after(job, st) if st["kind"] == "approval" else self.save(job, stage=st["id"], note="")

    def rework(self, job_ref: str, note: str, scope: bool = False) -> dict[str, Any]:
        job = self.get(job_ref)
        k = jobs.next_rework(self.root, job["id"])
        (self.dir(job) / f"rework-{k}.md").write_text(note.strip() + "\n")
        st, waiting = self.current(job)
        first_approval = self.stage_of_kind("approval")
        if waiting and st is not None and first_approval and st["id"] == first_approval["id"]:
            before = self.path[: self.index(st["id"])]
            rerun = [s for s in before if role(s) == "plain" and (
                s.get("rerun_on_rework") == "always" or (scope and s.get("rerun_on_rework") == "scope"))]
            if not rerun:
                rerun = [s for s in before if role(s) == "plain"][-1:]
            job = self.save(job, rework=k)
            return self.enter(job, rerun[0]["id"]) if rerun else job
        if job["stage"] in ("needs-human", "ready") or (waiting and st and st["kind"] == "delivery"):
            builders = self.of_role("builder")
            if not builders:
                raise FactoryError("this loop has no builder to rework")
            job = self.save(job, rework=k, round_base=job["round"], fresh_builders=True, rebuild=None,
                            round_due=True, builders=job["builders"] or [x["id"] for x in builders])
            return self.enter(job, builders[0]["id"])
        raise FactoryError(f"rework applies to a job awaiting its first approval, ready or needing a human; "
                           f"{job['id']} is in {job['stage']}")

    def deliver(self, job_ref: str, by: str) -> dict[str, Any]:
        job = self.get(job_ref)
        st, waiting = self.current(job)
        dstage = self.delivery
        if self.cfg["delivery"]["mode"] == "none":
            raise FactoryError("delivery.mode is none: push the branch and open the PR yourself")
        if job["stage"] == "ready" and self.cfg["delivery"]["mode"] != "none":
            job = self.enter(job, dstage["id"])  # e.g. delivery switched on after the job finished
            st, waiting = self.current(job)
        if st is None or st["id"] != dstage["id"]:
            raise FactoryError(f"job {job['id']} is in {job['stage']}, not ready to deliver")
        if waiting:
            if not jobs.approved(self.root, job, st["id"]):
                raise FactoryError(f"delivery needs an approval first: factory approve {job['id']}")
            job = self.save(job, stage=st["id"])
        job = self.save(job, agent="deliver")
        try:
            result = deliv.deliver(self.cfg, job, self.dir(job))
        except FactoryError as e:
            self.needs_human(job, f"delivery failed: {e}")
            raise
        result.update({"at": now(), "by": by})
        notes = self._ticket_write_back(job, result)
        return self.save(job, stage="delivered", agent="", delivery=result,
                         note=(result.get("url") or result["detail"]) + (f"; {notes}" if notes else ""))

    def _ticket_write_back(self, job: dict[str, Any], result: dict[str, Any]) -> str:
        conn = self.cfg["connections"]["tickets"]
        if not job.get("ticket") or conn.get("provider") != "jira" or conn.get("write_back") in ("none", "draft"):
            return ""
        try:
            jira = tix.Jira(conn)
            comment = self.dir(job) / "jira-comment.md"
            text = comment.read_text() if comment.is_file() else f"Delivered by the software factory: {result['detail']}"
            if result.get("url"):
                text += f"\n\n{result['url']}"
            jira.comment(job["ticket"], text)
            if conn.get("write_back") == "transition" and conn.get("transition_to"):
                jira.transition(job["ticket"], conn["transition_to"])
            return f"commented on {job['ticket']}"
        except FactoryError as e:
            return f"Jira write-back failed: {e}"

    def abandon(self, job_ref: str, force: bool = False) -> dict[str, Any]:
        job = self.get(job_ref)
        wt = Path(job["worktree"])
        if wt.exists():
            argv = ["git", "-C", str(self.root), "worktree", "remove", str(wt)] + (["--force"] if force else [])
            r = run(argv, check=False)
            if r.returncode != 0:
                raise FactoryError(f"worktree has changes; look at `git -C {wt} status`, then abandon --force")
        pushed = git("branch", "-r", "--contains", job["branch"], cwd=self.root, check=False)
        if not pushed:
            run(["git", "-C", str(self.root), "branch", "-D", job["branch"]], check=False)
        return self.save(job, stage="abandoned", agent="",
                         note="branch kept (it was pushed)" if pushed else "worktree and branch removed")


def _verdict(path: Path) -> str:
    line = first_line(path).upper()
    if "VERDICT" in line:
        if "CHANGES" in line:
            return "CHANGES"
        if "PASS" in line:
            return "PASS"
    return ""
