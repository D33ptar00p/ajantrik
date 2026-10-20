"""The headless edition: drive the loop without an interactive session.

`factory run "<request>"` creates a job and runs it through the configured backend (claude-cli,
codex-cli or api) until it needs a human: an approval stage, needs-human, ready or delivered.
`factory go <job>` approves and continues; `factory resume <job>` continues.

The runner does what the /factory skill tells Claude Code's orchestrator to do, in code: ask the
engine for the next step, run the agents it names (parallel where the step says so), and hand
back to the engine. Same job folder, same job.json, same dashboard.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ajantrik.core import jobs
from ajantrik.core.engine import Engine
from ajantrik.core.util import FactoryError, read_json, write_json
from ajantrik.editions.headless.backends.base import AgentResult, AgentRun, Backend
from ajantrik.editions.headless.backends.cli_agents import ClaudeCliBackend, CodexCliBackend
from ajantrik.editions.headless.backends.llm_api import LlmApiBackend
from ajantrik.kit.project import AgentDef, Project, SkillDef
from ajantrik.kit.render import resolve_model
from ajantrik.kit.schema import FactoryConfig

FRAMING = """You are the `{agent}` agent of a software factory working on the {repo} repository. An
orchestrator gives you one task; you do it and write the file it names. Your instructions follow.
Tools: {tools}. Paths can be absolute; relative paths start in the job worktree. Every command and
file access is checked by the factory guard: a `factory guard: blocked` result is a rule, not an
error to work around; report it in your output file instead.
"""


def build_backend(cfg: FactoryConfig) -> Backend:
    rt = cfg.runtime
    if rt.kind == "claude-cli":
        return ClaudeCliBackend(rt.claude_cli.binary, rt.claude_cli.permission_mode, rt.claude_cli.extra_args)
    if rt.kind == "codex-cli":
        return CodexCliBackend(rt.codex_cli.binary, rt.codex_cli.sandbox, rt.codex_cli.extra_args)
    if rt.kind == "api":
        return LlmApiBackend(rt.api.model_dump())
    raise FactoryError("runtime.kind is claude-code: run /factory in a Claude Code session in the repo, or pick "
                       "claude-cli, codex-cli or api to run headless")


def system_prompt(cfg: FactoryConfig, agent: AgentDef, skills: dict[str, SkillDef]) -> str:
    parts = [FRAMING.format(agent=agent.name, repo=cfg.repo.name, tools=", ".join(agent.tools)),
             "Read these repo files first when they exist: " + ", ".join(cfg.repo.context_files) + ".",
             "", agent.body.strip()]
    for name in agent.skills:
        s = skills.get(name)
        if s:
            parts += ["", f"## Skill: {s.name}", s.description, "", s.body.strip()]
    return "\n".join(parts)


class Runner:
    def __init__(self, project: Project, backend: Backend | None = None,
                 say: Callable[[str], None] = print, wait_for_approval: bool = False, poll_s: float = 5.0):
        loaded = project.load()
        if not loaded.ok or loaded.cfg is None:
            raise FactoryError("config has errors: " + "; ".join(loaded.errors))
        self.project = project
        self.cfg = loaded.cfg
        self.agents = loaded.agents
        self.skills = loaded.skills
        self.engine = Engine(project.root)
        self.backend = backend or build_backend(self.cfg)
        self.say = say
        self.wait = wait_for_approval
        self.poll_s = poll_s

    # ---- sessions: resume a builder in later rounds ---------------------------------------------
    def _sessions_path(self, job_id: str) -> Path:
        return jobs.job_dir(self.project.root, job_id) / "agents" / "sessions.json"

    def _session(self, job_id: str, name: str) -> str | None:
        return (read_json(self._sessions_path(job_id), {}) or {}).get(name)

    def _remember(self, job_id: str, name: str, session: str | None) -> None:
        if not session:
            return
        path = self._sessions_path(job_id)
        data = read_json(path, {}) or {}
        data[name] = session
        write_json(path, data)

    # ---- one agent --------------------------------------------------------------------------------
    async def run_agent(self, job: dict[str, Any], spec: dict[str, Any], resume: bool) -> AgentResult:
        agent = self.agents.get(spec["agent"])
        if agent is None:
            return AgentResult(False, error=f"no agent file for {spec['agent']}")
        session = self._session(job["id"], spec["name"]) if resume and self.backend.supports_resume else None
        prompt: str = spec["resume_message"] if session and spec.get("resume_message") else spec["prompt"]
        jd = jobs.job_dir(self.project.root, job["id"])
        logs = jd / "agents"
        logs.mkdir(parents=True, exist_ok=True)
        run = AgentRun(name=spec["name"], agent=agent.name, system=system_prompt(self.cfg, agent, self.skills),
                       prompt=prompt, model=resolve_model(self.cfg, agent), tools=agent.tools, root=self.project.root,
                       worktree=Path(job["worktree"]), job_dir=jd, session_id=session,
                       log_path=logs / f"{spec['name']}.r{job['round']}.jsonl")
        self.say(f"{job['id']}: {agent.name} ({self.backend.name}, {run.model})" + (" resumed" if session else ""))
        result = await self.backend.run(run)
        self._remember(job["id"], spec["name"], result.session_id)
        if not result.ok:
            self.say(f"{job['id']}: {agent.name} failed: {result.error}")
        return result

    async def run_agents(self, job: dict[str, Any], action: dict[str, Any]) -> None:
        sem = asyncio.Semaphore(self.cfg.runtime.concurrency if action.get("parallel") else 1)
        reviewers = self.engine.current(job)[0]
        is_review = bool(reviewers and reviewers["kind"] == "reviewers")

        async def one(spec: dict[str, Any]) -> AgentResult:
            async with sem:
                r = await self.run_agent(job, spec, bool(action.get("resume")))
            if is_review:
                self.engine.refresh(job["id"])
            return r

        results = await asyncio.gather(*(one(s) for s in action["agents"]))
        failed = [s["agent"] for s, r in zip(action["agents"], results, strict=True) if not r.ok]
        if failed:
            # The files decide: an agent that crashed after writing its file still counts. The engine
            # sends the job to a human when an expected file is missing.
            self.say(f"{job['id']}: {', '.join(failed)} did not finish cleanly; checking what they wrote")

    # ---- the loop ---------------------------------------------------------------------------------
    async def drive(self, job_id: str) -> dict[str, Any]:
        eng = self.engine
        while True:
            action = eng.step(job_id)
            job = eng.get(job_id)
            kind = action["type"]
            if kind == "agents":
                await self.run_agents(job, action)
                job = eng.advance(job_id)
                self.say(f"{job_id}: → {job['stage']}" + (f" ({job['note']})" if job.get("note") else ""))
            elif kind == "advance":
                eng.advance(job_id)
            elif kind == "gate":
                self.say(f"{job_id}: gate round {job['round']}")
                job = await asyncio.to_thread(eng.run_gate, job_id)
                self.say(f"{job_id}: gate → {job['stage']}" + (f" ({job['note']})" if job.get("note") else ""))
            elif kind == "deliver":
                try:
                    job = eng.deliver(job_id, os.environ.get("FACTORY_APPROVER", "factory"))
                    self.say(f"{job_id}: delivered: {job['note']}")
                except FactoryError as e:  # the engine already moved the job to needs-human
                    self.say(f"{job_id}: delivery failed: {e}")
            elif kind == "approval":
                if not self.wait:
                    self.say(f"{job_id}: waiting for you ({action['note']}): factory go {job_id}"
                             if action["kind"] == "approval" else
                             f"{job_id}: waiting for you ({action['note']}): factory approve {job_id}")
                    return job
                await asyncio.sleep(self.poll_s)
            else:
                if action.get("commands"):
                    self.say("\n".join(action["commands"]))
                return job

    def run(self, request: str = "", ticket: str | None = None, base: str | None = None) -> dict[str, Any]:
        job = self.engine.new_job(request, ticket, base)
        self.say(f"{job['id']}: created on {job['branch']} (base {job['base']}), worktree {job['worktree']}")
        return asyncio.run(self.drive(job["id"]))

    def go(self, job_ref: str, by: str) -> dict[str, Any]:
        job = self.engine.get(job_ref)
        if job["stage"].startswith("awaiting-"):
            job = self.engine.approve(job["id"], by)
        return asyncio.run(self.drive(job["id"]))

    def resume(self, job_ref: str) -> dict[str, Any]:
        return asyncio.run(self.drive(self.engine.get(job_ref)["id"]))


def dump(job: dict[str, Any]) -> str:
    return json.dumps(job, indent=2)
