"""The factory.yaml schema.

One file defines a factory: the repo, the runtime that drives the agents, the loop (stages, rounds,
approvals and gates, each agent in a plan, build, review or integrate phase), the gate, connections (git host, tickets) and how
a finished job is delivered. Agents and skills are markdown files next to it (see project.py).

`render` writes the validated model, with every default filled in, to `.factory/factory.json`. The
stdlib-only runtime in ajantrik/core (hooks, gate, engine, dashboard) reads that file, so defaults live
here and only here.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ajantrik.core.graph import DELIVER, END, groups, role_for_agent, run_order, successor

SECRET_REF = re.compile(r"^(env|keyring):[\w./-]+$")
ID = re.compile(r"^[a-z][a-z0-9-]*$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _ref(v: str | None) -> str | None:
    """Credentials are referenced (env:NAME, keyring:service/name), never written into the yaml."""
    if v is None or v == "":
        return None
    if not SECRET_REF.match(v):
        raise ValueError("must be a reference like env:JIRA_TOKEN or keyring:factory/jira, never the value itself")
    return v


# ---- repo -----------------------------------------------------------------------------------------
class Repo(_Model):
    name: str = Field(description="Repository name, used in titles, worktree paths and messages.")
    title: str = Field("", description="Dashboard title. Defaults to '<name> Software Factory'.")
    default_base: str = Field("main", description="Base branch when neither the ticket nor --base gives one.")
    worktree_root: str = Field(
        "../{repo}-factory", description="Where job worktrees go, relative to the repo root. {repo} is the name."
    )
    context_files: list[str] = Field(
        default_factory=lambda: ["CLAUDE.md", "AGENTS.md"],
        description="Repo files every agent reads first for setup, commands and conventions.",
    )

    @model_validator(mode="after")
    def _title(self) -> Repo:
        if not self.title:
            self.title = f"{self.name} Software Factory"
        return self


# ---- runtime --------------------------------------------------------------------------------------
RuntimeKind = Literal["claude-code", "claude-cli", "codex-cli", "api"]
ApiProvider = Literal["anthropic", "openai", "bedrock", "ollama", "litellm", "openai-compatible"]


class ApiRuntime(_Model):
    provider: ApiProvider = "anthropic"
    base_url: str | None = Field(
        None, description="Ollama (default http://localhost:11434), LiteLLM proxy or any OpenAI-compatible server."
    )
    api_key_ref: str | None = Field(None, description="env:NAME or keyring:service/name. Not needed for Ollama.")
    temperature: float | None = None
    max_turns: int = Field(80, ge=1, description="Tool-calling turns per agent run.")
    tool_output_limit: int = Field(20000, ge=1000, description="Characters of tool output returned to the model.")
    command_timeout_s: int = Field(600, ge=10)
    extra: dict[str, str | int | float | bool] = Field(default_factory=dict, description="Passed to litellm as-is.")

    @field_validator("api_key_ref")
    @classmethod
    def _refs(cls, v: str | None) -> str | None:
        return _ref(v)


class ClaudeCliRuntime(_Model):
    binary: str = "claude"
    permission_mode: Literal["default", "acceptEdits", "bypassPermissions"] = "acceptEdits"
    extra_args: list[str] = Field(default_factory=list)


class CodexCliRuntime(_Model):
    binary: str = "codex"
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    extra_args: list[str] = Field(default_factory=list)


class Runtime(_Model):
    kind: RuntimeKind = Field(
        "claude-code",
        description="claude-code: interactive /factory skill. claude-cli / codex-cli / api: `factory run` drives "
        "the loop headless through that backend.",
    )
    models: dict[str, str] = Field(
        default_factory=dict,
        description="Model aliases used in agent files (opus, sonnet, haiku...) mapped to this runtime's model "
        "ids, e.g. {opus: claude-opus-5-5} or {opus: llama3.2} for Ollama. Unmapped aliases pass through.",
    )
    default_model: str = Field("sonnet", description="Alias or id for agents that name no model.")
    per_agent: dict[str, str] = Field(default_factory=dict, description="Agent name -> model, overriding its file.")
    concurrency: int = Field(4, ge=1, le=16, description="Agents run at once in a parallel stage (headless).")
    api: ApiRuntime = Field(default_factory=ApiRuntime)
    claude_cli: ClaudeCliRuntime = Field(default_factory=ClaudeCliRuntime)
    codex_cli: CodexCliRuntime = Field(default_factory=CodexCliRuntime)


# ---- loop -----------------------------------------------------------------------------------------
# Every block in the loop is an agent, an approval or a gate; there are no teams. What an agent stage
# does comes from its agent (graph.role_of). A phase is a board column and nothing more; agents and
# gates sit in one, a job waiting for a person shows under Needs human. Parallelism comes from the
# edges: a stage whose `next` names several agents fans out, and they run at once (see graph.groups).
StageKind = Literal["agent", "approval", "gate"]
AgentRole = Literal["plain", "builder", "reviewer", "integrator"]

# Fields that only mean something for one role, so a stray one is an error rather than ignored.
BUILDER_FIELDS = ("area_globs", "description")
REVIEWER_FIELDS = ("chip", "path_globs", "exclude_globs", "diff_patterns", "scoped_patterns", "metrics", "builder")


class Pos(_Model):
    x: float
    y: float


class Phase(_Model):
    id: str = Field(description="Phase id, referenced by a stage's `phase`.")
    title: str = Field("", description="Board column heading. Default: the id, capitalised.")

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID.match(v):
            raise ValueError("phase ids are lower-case kebab: [a-z][a-z0-9-]*")
        return v

    @model_validator(mode="before")
    @classmethod
    def _drop_parallel(cls, data: object) -> object:
        # Parallelism used to be a phase setting; it now comes from the loop's edges.
        if isinstance(data, dict) and "parallel" in data:
            data = {k: v for k, v in data.items() if k != "parallel"}
        return data

    @model_validator(mode="after")
    def _title(self) -> Phase:
        self.title = self.title or self.id.replace("-", " ").capitalize()
        return self


class ScopedPattern(_Model):
    paths: list[str] = Field(description="Only added lines in files matching these globs count.")
    pattern: str = Field(description="Python regex searched in each added line.")


class MetricRule(_Model):
    key: str = Field(description="gate.json key, e.g. data_mb_delta.")
    gt: float


class Stage(_Model):
    id: str = Field(description="Stage id, also the job.json `stage` value (approval stages: awaiting-<id>). "
                    "A builder's id names its build-<id>.md; a reviewer's its review-<id>.md.")
    kind: StageKind
    title: str = ""
    phase: str | None = Field(
        None, description="Board column (a loop.phases id). Agents default to the first phase, gates to guardrails "
        "(else review, else the last). Approvals have none: a job waiting for a person shows under Needs human.")
    role: AgentRole | None = Field(
        None, description="kind agent: plain (writes `output`), builder (code and build-<id>.md, rebuilt on "
        "findings), reviewer (VERDICT: PASS|CHANGES each round) or integrator (READY|ESCALATE). Default: from the "
        "agent's name (builder-*, reviewer-*, integrator), else plain.")
    next: str | list[str] | None = Field(
        None, description="Success edge: the stage that follows. Unset: the next one in the list. `end`: done. "
        "A list fans out: those agents run at the same time and must all lead to one stage.")
    pos: Pos | None = Field(None, description="Where the loop designer draws this stage. Layout only.")
    agent: str | None = Field(None, description="kind agent: the agent file. Default builder-<id> / reviewer-<id>.")
    output: str | None = Field(
        None, description="plain/integrator: file the agent writes in the job folder (default <id>.md / decision.md).")
    prompt: str = Field("", description="Extra instructions appended to the agent prompt for this stage.")
    read_builders: bool = Field(False, description="Read the `Builders:` line of the output into job.builders.")
    on_fail: str | None = Field(
        None, description="gate/reviewer: stage to return to (default: the nearest builder before it).")
    note: str = Field("", description="approval: what the card says while waiting.")
    steps: list[str] = Field(default_factory=list, description="gate: the guardrails it runs: gate.steps keys and "
                             "the built-in `secrets` and `clean-tree`. Empty: all of them.")
    rerun_on_rework: Literal["always", "scope", "never"] = Field(
        "never", description="plain agents before the first approval: re-run on rework (scope = spec changes only)."
    )
    # builder
    area_globs: list[str] = Field(default_factory=list, description="builder: paths it owns.")
    description: str = Field("", description="builder: what it builds, shown to the architect.")
    # reviewer
    chip: str | None = Field(None, description="reviewer: dashboard chip label. Default: the id.")
    path_globs: list[str] = Field(default_factory=list, description="reviewer: run when any changed path matches.")
    exclude_globs: list[str] = Field(default_factory=list, description="reviewer: changed paths that never trigger it.")
    diff_patterns: list[str] = Field(default_factory=list, description="reviewer: run when an added line matches.")
    scoped_patterns: list[ScopedPattern] = Field(default_factory=list)
    metrics: list[MetricRule] = Field(default_factory=list, description="reviewer: run when a gate metric exceeds a value.")
    builder: str | None = Field(None, description="reviewer: builder that gets its findings (default: every builder).")

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID.match(v):
            raise ValueError("stage ids are lower-case kebab: [a-z][a-z0-9-]*")
        return v

    @property
    def routed(self) -> bool:
        """A reviewer with triggers runs only when one matches; without any it always runs."""
        return bool(self.path_globs or self.diff_patterns or self.scoped_patterns or self.metrics)

    @model_validator(mode="after")
    def _shape(self) -> Stage:
        if self.kind != "agent":
            for f in ("agent", "output", "read_builders", "role", *BUILDER_FIELDS, *REVIEWER_FIELDS):
                if getattr(self, f):
                    raise ValueError(f"stage {self.id}: `{f}` is for agent stages")
        else:
            self.role = self.role or role_for_agent(self.agent)  # type: ignore[assignment]
        if self.kind == "approval" and self.phase:
            raise ValueError(f"stage {self.id}: approvals have no phase (they show under Needs human)")
        if self.kind != "gate" and self.steps:
            raise ValueError(f"stage {self.id}: `steps` is for gate stages")
        if self.on_fail and not (self.kind == "gate" or self.role == "reviewer"):
            raise ValueError(f"stage {self.id}: only gate and reviewer stages have `on_fail`")
        if self.role != "builder" and any(getattr(self, f) for f in BUILDER_FIELDS):
            raise ValueError(f"stage {self.id}: area_globs and description are for builders")
        if self.role != "reviewer" and any(getattr(self, f) for f in REVIEWER_FIELDS):
            raise ValueError(f"stage {self.id}: routing fields are for reviewers")
        if self.kind != "agent":
            return self
        if self.role in ("builder", "reviewer") and self.output:
            raise ValueError(f"stage {self.id}: a {self.role} writes its own file, not `output`")
        if self.role == "builder":
            self.agent = self.agent or f"builder-{self.id}"
        elif self.role == "reviewer":
            self.agent = self.agent or f"reviewer-{self.id}"
            self.chip = self.chip or {"security": "sec"}.get(self.id, self.id)
        elif not self.agent:
            raise ValueError(f"stage {self.id}: an agent stage needs `agent`")
        if self.role == "integrator":
            self.output = self.output or "decision.md"
        elif self.role == "plain":
            self.output = self.output or f"{self.id}.md"
        return self


def default_phases() -> list[Phase]:
    return [Phase(id=p) for p in ("plan", "build", "guardrails", "review", "integrate")]


def default_stages() -> list[Stage]:
    return [
        Stage(id="spec", kind="agent", agent="spec-writer", output="spec.md", title="Spec", phase="plan",
              rerun_on_rework="scope"),
        Stage(id="design", kind="agent", agent="architect", output="design.md", title="Design", phase="plan",
              read_builders=True, rerun_on_rework="always"),
        Stage(id="approval", kind="approval", title="Approve", note="approve the spec and design"),
        Stage(id="tests", kind="agent", agent="test-author", output="tests.md", title="Tests", phase="build"),
        Stage(id="main", kind="agent", role="builder", title="Build", phase="build"),
        Stage(id="gate", kind="gate", title="Guardrails", phase="guardrails", on_fail="main",
              next=["code", "security"]),
        Stage(id="code", kind="agent", role="reviewer", title="Code review", phase="review", on_fail="main",
              next="integrate"),
        Stage(id="security", kind="agent", role="reviewer", title="Security review", phase="review",
              on_fail="main"),
        Stage(id="integrate", kind="agent", agent="integrator", title="Integrate", phase="integrate"),
    ]


class Loop(_Model):
    max_rounds: int = Field(3, ge=1, le=10, description="A round is one gate run plus, if it passes, one review.")
    start: str | None = Field(None, description="The first stage a job enters. Default: the first in the list.")
    phases: list[Phase] = Field(default_factory=default_phases,
                                description="Board columns, in order, before Done and Needs human.")
    stages: list[Stage] = Field(default_factory=default_stages)

    @field_validator("phases")
    @classmethod
    def _phases(cls, v: list[Phase]) -> list[Phase]:
        ids = [p.id for p in v]
        if not ids:
            raise ValueError("the loop needs at least one phase")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate phases")
        return v

    @model_validator(mode="after")
    def _default_phase(self) -> Loop:
        ids = [p.id for p in self.phases]
        for s in self.stages:
            if s.phase is None and s.kind == "agent":
                s.phase = ids[0]
            elif s.phase is None and s.kind == "gate":
                s.phase = next((p for p in ("guardrails", "review") if p in ids), ids[-1])
            if s.phase and s.phase not in ids:
                raise ValueError(f"stage {s.id}: phase {s.phase!r} is not in loop.phases")
        return self

    def order(self) -> list[str]:
        """Stage ids in the order a job visits them (the path of success edges from the start)."""
        return run_order(self.stages, self.start)

    def ordered(self) -> list[Stage]:
        by_id = {s.id: s for s in self.stages}
        return [by_id[i] for i in self.order()]

    def of_role(self, role: str) -> list[Stage]:
        """Agent stages with this role, in run order."""
        return [s for s in self.ordered() if s.kind == "agent" and s.role == role]

    def groups(self) -> list[list[Stage]]:
        """The run path in steps: the branches of a fan-out run as one step (graph.groups, as the engine)."""
        by_id = {s.id: s for s in self.stages}
        return [[by_id[i] for i in g] for g in groups(self.stages, self.start)]


# ---- version 1 -> 2 ---------------------------------------------------------------------------------
# Version 1 had `builders` and `reviewers` teams at the top level, stage kinds builders / reviewers /
# integrate / delivery, and no phases. `migrate` turns it into version 2: each builder becomes a build
# agent and each reviewer a review agent; agents before the first approval go to plan, the ones
# between it and the builders to build and the ones after to review (each keeping its output file);
# the gate goes to review; and every gate and reviewer gets an explicit failure edge to the first
# builder, where version 1 sent failures implicitly.
_V1_KINDS = {"builders", "reviewers", "integrate", "delivery"}
_V1_STAGES: list[dict[str, Any]] = [
    {"id": "spec", "kind": "agent", "agent": "spec-writer", "output": "spec.md", "title": "Spec",
     "rerun_on_rework": "scope"},
    {"id": "design", "kind": "agent", "agent": "architect", "output": "design.md", "title": "Design",
     "read_builders": True, "rerun_on_rework": "always"},
    {"id": "approval", "kind": "approval", "title": "Approve", "note": "approve the spec and design"},
    {"id": "tests", "kind": "agent", "agent": "test-author", "output": "tests.md", "title": "Tests"},
    {"id": "build", "kind": "builders", "title": "Build"},
    {"id": "gate", "kind": "gate", "title": "Gate"},
    {"id": "review", "kind": "reviewers", "title": "Review"},
    {"id": "integrate", "kind": "integrate", "agent": "integrator", "title": "Integrate"},
    {"id": "deliver", "kind": "delivery", "title": "Deliver"},
]


def is_v1(data: dict) -> bool:
    if data.get("version") == 1:
        return True
    if data.get("version") is not None:
        return False
    stages = (data.get("loop") or {}).get("stages") or []
    return "builders" in data or "reviewers" in data or any(
        isinstance(s, dict) and s.get("kind") in _V1_KINDS for s in stages)


def _kebab(v: str) -> str:
    out = re.sub(r"[^a-z0-9-]+", "-", str(v).lower()).strip("-")
    return out if out and out[0].isalpha() else f"x-{out}"


def migrate(data: dict) -> dict:
    """A version 1 factory.yaml mapping as version 2. Anything else is returned unchanged."""
    if not isinstance(data, dict) or not is_v1(data):
        return data
    data = copy.deepcopy(data)
    loop = dict(data.get("loop") or {})
    old: list[dict[str, Any]] = [dict(s) for s in (loop.get("stages") or _V1_STAGES)]
    builders = [dict(b) for b in data.pop("builders", None) or [{"id": "main"}]]
    rv = data.pop("reviewers", None) or {"always": ["code", "security"]}
    always = [{"id": r} if isinstance(r, str) else dict(r) for r in rv.get("always") or []]
    routed = [dict(r) for r in rv.get("routed") or []]
    ui = data.get("ui")
    if isinstance(ui, dict) and "build_label" in ui:
        data["ui"] = {k: v for k, v in ui.items() if k != "build_label"}

    try:
        order = run_order(old, loop.get("start"))
    except ValueError:
        order = [s["id"] for s in old]
    kinds = {s["id"]: s.get("kind") for s in old}
    first_approval = next((i for i, sid in enumerate(order) if kinds[sid] == "approval"), len(order))
    plan = {sid for sid in order[:first_approval] if kinds[sid] == "agent"}
    bpos = next((i for i, sid in enumerate(order) if kinds[sid] == "builders"), None)

    def phase_of(sid: str) -> str:
        if sid in plan or bpos is None or sid not in order:
            return "plan"
        return "build" if order.index(sid) < bpos else "review"

    taken = {s["id"] for s in old if s.get("kind") not in ("builders", "reviewers")}

    def fresh(base: str, prefix: str) -> str:
        sid = _kebab(base)
        if sid in taken:
            sid = f"{prefix}-{sid}"
        n = 2
        while sid in taken:
            sid, n = f"{prefix}-{_kebab(base)}-{n}", n + 1
        taken.add(sid)
        return sid

    bmap = {b["id"]: fresh(b["id"], "build") for b in builders}
    rmap = {r["id"]: fresh(r["id"], "review") for r in [*always, *routed]}
    renamed: dict[str, str] = {}  # old stage id -> the id that replaces it
    deliver_ids = {s["id"] for s in old if s.get("kind") == "delivery"}

    ordered_old = [s for sid in order for s in old if s["id"] == sid] + [s for s in old if s["id"] not in order]
    position = {s["id"]: i for i, s in enumerate(old)}
    new_by_old: dict[str, list[dict]] = {}
    for s in ordered_old:
        kind = s.get("kind")
        sid = s["id"]
        common = {k: s[k] for k in ("pos",) if s.get(k)}
        if kind == "delivery":
            new_by_old[sid] = []
        elif kind == "approval":
            new_by_old[sid] = [{k: v for k, v in s.items()}]
        elif kind == "gate":
            g = {k: v for k, v in s.items()}
            g["phase"] = "guardrails"
            new_by_old[sid] = [g]
        elif kind == "agent":
            a = {k: v for k, v in s.items()}
            a["phase"] = phase_of(sid)
            new_by_old[sid] = [a]
        elif kind == "integrate":
            a = {k: v for k, v in s.items() if k != "kind"}
            a.update(kind="agent", phase="integrate")
            if a.get("output") == "decision.md":
                del a["output"]
            new_by_old[sid] = [a]
        elif kind == "builders":
            out = []
            for b in builders:
                st = {"id": bmap[b["id"]], "kind": "agent", "phase": "build", "title": b["id"]}
                if b.get("agent") and b["agent"] != f"builder-{bmap[b['id']]}":
                    st["agent"] = b["agent"]
                elif not b.get("agent") and bmap[b["id"]] != b["id"]:
                    st["agent"] = f"builder-{b['id']}"
                for f in ("area_globs", "description"):
                    if b.get(f):
                        st[f] = b[f]
                if s.get("prompt"):
                    st["prompt"] = s["prompt"]
                out.append(st)
            new_by_old[sid] = out
        elif kind == "reviewers":
            out = []
            for r in [*always, *routed]:
                st = {"id": rmap[r["id"]], "kind": "agent", "phase": "review", "title": r["id"]}
                default_agent = f"reviewer-{r['id']}"
                agent = r.get("agent") or default_agent
                if agent != f"reviewer-{rmap[r['id']]}":
                    st["agent"] = agent
                default_chip = {"security": "sec"}.get(r["id"], r["id"])
                chip = r.get("chip") or default_chip
                if chip != {"security": "sec"}.get(rmap[r["id"]], rmap[r["id"]]):
                    st["chip"] = chip
                for f in ("path_globs", "exclude_globs", "diff_patterns", "scoped_patterns", "metrics"):
                    if r.get(f):
                        st[f] = r[f]
                if r.get("builder"):
                    st["builder"] = bmap.get(r["builder"], r["builder"])
                for k in ("on_fail", "prompt"):
                    if s.get(k):
                        st[k] = s[k]
                out.append(st)
            new_by_old[sid] = out
        else:
            new_by_old[sid] = [{k: v for k, v in s.items()}]
        group = new_by_old[sid]
        if group:
            renamed[sid] = group[0]["id"]
            if kind in ("builders", "reviewers") and common.get("pos"):
                group[0]["pos"] = common["pos"]

    # A team of several becomes a fan-out: whatever led into it now points at all its members, and each
    # member leads where the team did. Delivery is no longer a stage: whatever led into it ends the loop.
    teams = {sid: [s["id"] for s in g] for sid, g in new_by_old.items() if kinds[sid] in ("builders", "reviewers")}

    def target(ref: str | None) -> str | list[str] | None:
        if ref is None or ref == END:
            return ref
        if ref in deliver_ids:
            return END
        if len(teams.get(ref, [])) > 1:
            return list(teams[ref])
        return renamed.get(ref, ref)

    for s in old:
        group = new_by_old[s["id"]]
        if not group:
            continue
        nxt = target(successor(old, s["id"])) or END
        for st in group:
            st.pop("next", None)
        for st in group if s["id"] in teams else group[-1:]:
            st["next"] = nxt

    stages: list[dict] = []
    for s in sorted(old, key=lambda s: position[s["id"]]):
        stages.extend(new_by_old[s["id"]])
    # Keep `next` only where it differs from the list order, as the designer writes it.
    for i, s in enumerate(stages):
        following = stages[i + 1]["id"] if i + 1 < len(stages) else END
        if s.get("next") == following:
            del s["next"]
        if s.get("on_fail"):
            s["on_fail"] = renamed.get(s["on_fail"], s["on_fail"])
    if loop.get("start"):
        loop["start"] = renamed.get(loop["start"], loop["start"])
    first_builder = bmap[builders[0]["id"]] if bpos is not None and builders else None
    if first_builder:
        for sid, group in new_by_old.items():
            if sid not in order or order.index(sid) < bpos:  # type: ignore[operator]
                continue
            for s in group:
                checks = s["kind"] == "gate" or kinds[sid] == "reviewers"
                if checks and not s.get("on_fail"):
                    s["on_fail"] = first_builder
    intended = {"builders": "builder", "reviewers": "reviewer", "integrate": "integrator", "agent": "plain"}
    for sid, group in new_by_old.items():
        role = intended.get(kinds[sid] or "")
        for s in group:
            if role and s["kind"] == "agent" and role_for_agent(s.get("agent")) != role:
                s["role"] = role
    fields = list(Stage.model_fields)
    loop["stages"] = [dict(sorted(s.items(), key=lambda kv: fields.index(kv[0]) if kv[0] in fields else 99))
                      for s in stages]
    data["loop"] = {k: loop[k] for k in ("max_rounds", "start", "stages") if k in loop} | loop
    for g in ((data.get("gate") or {}).get("steps") or []):
        if isinstance(g, dict) and g.get("builder"):
            g["builder"] = bmap.get(g["builder"], g["builder"])
    data["version"] = 2
    return data


# ---- gate -----------------------------------------------------------------------------------------
class Coverage(_Model):
    cobertura: str | None = Field(None, description="Cobertura XML path (relative to the round folder).")
    json_summary: str | None = Field(None, description="istanbul coverage-summary.json path.")


class GateStep(_Model):
    key: str = Field(description="gate.json key, PASS/FAIL/skipped.")
    chip: str | None = Field(None, description="Dashboard chip label; default the key.")
    cmd: str = Field(description="bash, run in the worktree. Env: WORKTREE BASE MERGE_BASE OUT REPO FACTORY_DIR "
                     "CHANGED (file with changed paths).")
    when_changed: list[str] = Field(default_factory=list, description="Skip unless a changed path matches.")
    requires: list[str] = Field(default_factory=list, description="Skip when one of these steps did not PASS.")
    mode: Literal["gate", "report", "off"] = Field("gate", description="report: runs, never fails the gate.")
    timeout_min: float = 20
    lock: bool = Field(False, description="Hold .factory/.port.lock (serialises app servers across jobs).")
    env: dict[str, str] = Field(default_factory=dict)
    junit: str | None = Field(None, description="JUnit XML (relative to the round folder) for passed/failed.")
    coverage: Coverage | None = None
    metrics_file: str | None = Field(None, description="JSON file of extra numbers merged into gate.json.")
    external_failure: Literal["fail", "needs-human"] = Field(
        "fail", description="needs-human: if only this step fails, stop without using a round (flaky e2e).",
    )
    builder: str | None = Field(None, description="Builder that gets this step's failures (default: all builders).")

    @model_validator(mode="after")
    def _chip(self) -> GateStep:
        self.chip = self.chip or self.key
        return self


class Check(_Model):
    test: str = Field(description="bash; non-zero exit fails the check.")
    error: str


# Guardrails a gate block can run besides gate.steps: what each runs is fixed (shown in the studio).
BUILTIN_GUARDRAILS = {
    "secrets": {"title": "Secret scan", "flag": "secrets",
                "description": "Every commit on the branch, with the built-in token patterns and this repo's .env "
                               "values (plus gitleaks when it is installed). Values are never written.",
                "cmd": '"$FACTORY_DIR/security/secret_scan.sh" "$OUT/secrets.json" --git "$WORKTREE" "$BASE"'},
    "clean-tree": {"title": "Clean tree", "flag": "clean_tree",
                   "description": "Fails when the worktree has uncommitted changes after the other guardrails ran.",
                   "cmd": 'test -z "$(git -C "$WORKTREE" status --porcelain)"'},
}


class Gate(_Model):
    setup: list[Check] = Field(default_factory=list, description="Checks before any step (venv, node_modules...).")
    steps: list[GateStep] = Field(default_factory=list, description="Guardrails: the checks a gate block can run.")
    secrets: bool = Field(True, description="The built-in secret scan guardrail: scan every commit on the branch.")
    clean_tree: bool = Field(True, description="The built-in clean-tree guardrail: no uncommitted changes afterwards.")
    timeout_min: float = 20


# ---- job setup ------------------------------------------------------------------------------------
class Preflight(_Model):
    cmd: str
    hint: str = ""


class WorktreeStep(_Model):
    kind: Literal["symlink", "copy", "run"]
    path: str | None = Field(None, description="symlink/copy: repo-relative path brought into the worktree.")
    cmd: str | None = Field(None, description="run: bash in the worktree.")

    @model_validator(mode="after")
    def _shape(self) -> WorktreeStep:
        if self.kind in ("symlink", "copy") and not self.path:
            raise ValueError(f"worktree step {self.kind} needs `path`")
        if self.kind == "run" and not self.cmd:
            raise ValueError("worktree step run needs `cmd`")
        return self


class Conventions(_Model):
    branch: str = Field("{type}/{key}_{words}", description="With a ticket. {type} {key} {num} {words}.")
    branch_no_ticket: str = Field("{type}/{num}_{words}")
    branch_types: dict[str, str] = Field(
        default_factory=lambda: {"Bug": "bugfix", "default": "feature"},
        description="Ticket type -> branch type.",
    )
    commit: str = Field("<type>: <KEY> - <what>", description="Commit message format agents follow.")
    pr_template: str | None = Field(None, description="Repo path of a PR template the integrator fills in.")


# ---- connections ----------------------------------------------------------------------------------
class GitConnection(_Model):
    provider: Literal["github", "gitlab", "bitbucket", "none"] = "github"
    remote: str = "origin"
    host: str | None = Field(None, description="GitHub Enterprise / self-hosted GitLab host.")
    token_ref: str | None = Field(None, description="Optional; gh/glab use their own login when unset.")
    bitbucket_workspace: str | None = None
    bitbucket_repo: str | None = None

    @field_validator("token_ref")
    @classmethod
    def _refs(cls, v: str | None) -> str | None:
        return _ref(v)


class TicketConnection(_Model):
    provider: Literal["jira", "none"] = "none"
    mode: Literal["rest", "mcp"] = Field(
        "rest", description="rest: the factory calls Jira itself. mcp: the Claude Code session's Atlassian MCP."
    )
    site: str | None = Field(None, description="https://<org>.atlassian.net")
    project: str | None = None
    label: str = "factory"
    jql: str | None = Field(None, description="Default: project = P AND labels = L AND statusCategory = 'To Do'.")
    email_ref: str | None = None
    token_ref: str | None = None
    epic_branch_glob: str = Field("*{epic}_*", description="Remote branch glob for an epic's base branch.")
    no_epic_branch: Literal["block", "fallback"] = "block"
    write_back: Literal["none", "draft", "comment", "transition"] = Field(
        "draft", description="draft: write jira-comment.md for a human. comment/transition: post on delivery."
    )
    transition_to: str | None = Field(None, description="Status to move the ticket to on delivery.")
    create_type: str = Field("Task", description="Issue type for backlog items pushed to Jira by sync --push.")
    key_pattern: str = r"[A-Z][A-Z0-9]+-\d+"

    @field_validator("email_ref", "token_ref")
    @classmethod
    def _refs(cls, v: str | None) -> str | None:
        return _ref(v)

    @model_validator(mode="after")
    def _jql(self) -> TicketConnection:
        if self.provider == "jira" and not self.jql and self.project:
            self.jql = (f'project = {self.project} AND labels = {self.label} AND statusCategory = "To Do" '
                        "ORDER BY rank ASC")
        return self

    @property
    def browse_url(self) -> str:
        return f"{self.site.rstrip('/')}/browse/" if self.site else ""


class Connections(_Model):
    git: GitConnection = Field(default_factory=GitConnection)
    tickets: TicketConnection = Field(default_factory=TicketConnection)


# ---- delivery -------------------------------------------------------------------------------------
class PrOptions(_Model):
    draft: bool = True
    reviewers: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class DirectOptions(_Model):
    strategy: Literal["ff-only", "squash"] = "ff-only"
    push: bool = Field(True, description="false: update the local base branch only (refused if it is checked out).")


class Delivery(_Model):
    mode: Literal["none", "push", "pr", "direct"] = Field(
        "none",
        description="none: stop at ready and print the commands. push: push the job branch. pr: push and open a "
        "PR/MR. direct: put the commits straight onto the base branch.",
    )
    require_approval: bool = Field(True, description="Wait for a human approval before delivering.")
    pr: PrOptions = Field(default_factory=PrOptions)
    direct: DirectOptions = Field(default_factory=DirectOptions)
    pos: Pos | None = Field(None, description="Where the loop designer draws the deliver block. Layout only.")


# ---- security, ui ---------------------------------------------------------------------------------
class GuardText(_Model):
    env_reader_scripts: str = Field("gate.sh", description="Named in the block message for .env reads.")
    local_url_example: str = "http://localhost:8080/"
    external_services: str = Field("Jira, Notion and Figma", description="Named in the MCP-write block message.")


class Security(_Model):
    refuse_paths: list[str] = Field(
        default_factory=lambda: [".claude/", ".factory/", ".github/workflows/"],
        description="A diff touching these goes to needs-human before integration.",
    )
    refuse_patterns: list[ScopedPattern] = Field(
        default_factory=list,
        description="Added lines that send the job to needs-human, e.g. new install scripts in package.json.",
    )
    outgoing_files: list[str] = Field(
        default_factory=lambda: ["pr.md", "pr-title.txt", "decision.md", "jira-comment.md"],
        description="Job files scanned for secrets before anything leaves the machine.",
    )
    npx_allow: list[str] = Field(
        default_factory=lambda: ["tsc", "eslint", "vitest", "vite", "cypress", "prettier", "playwright", "tsx",
                                 "ts-node", "jest"]
    )
    guard: GuardText = Field(default_factory=GuardText)


class Ui(_Model):
    port: int = Field(8000, ge=1024, le=65535)
    studio_port: int = Field(8765, ge=1024, le=65535)
    job_files: list[str] = Field(default_factory=list, description="Drawer file order. Default: from the loop.")
    skip_dirs: list[str] = Field(default_factory=lambda: ["mockup", "coverage", "e2e"])


class FactoryConfig(_Model):
    version: Literal[2] = 2
    repo: Repo
    runtime: Runtime = Field(default_factory=Runtime)
    loop: Loop = Field(default_factory=Loop)
    gate: Gate = Field(default_factory=Gate)
    preflight: list[Preflight] = Field(default_factory=list)
    worktree: list[WorktreeStep] = Field(default_factory=list)
    conventions: Conventions = Field(default_factory=Conventions)
    connections: Connections = Field(default_factory=Connections)
    delivery: Delivery = Field(default_factory=Delivery)
    security: Security = Field(default_factory=Security)
    ui: Ui = Field(default_factory=Ui)

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, data: object) -> object:
        return migrate(data) if isinstance(data, dict) else data

    @model_validator(mode="after")
    def _loop(self) -> FactoryConfig:
        validate_loop(self)
        return self

    # convenient lookups -----------------------------------------------------------------------
    def stage(self, sid: str) -> Stage | None:
        return next((s for s in self.loop.stages if s.id == sid), None)

    def agent_names(self) -> set[str]:
        """Every agent the config refers to."""
        return {s.agent for s in self.loop.stages if s.agent}


def validate_loop(cfg: FactoryConfig) -> None:
    stages = cfg.loop.stages
    ids = [s.id for s in stages]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate stage ids: {sorted(dupes)}")
    reserved = {"ready", "needs-human", "abandoned", "delivered", END, DELIVER}
    if reserved & set(ids):
        raise ValueError(f"stage ids {sorted(reserved & set(ids))} are reserved job states")
    for s in stages:
        nexts = s.next if isinstance(s.next, list) else [s.next] if s.next else []
        for n in nexts:
            if n != END and n not in ids:
                raise ValueError(f"stage {s.id}: next {n!r} is not a stage (or `end`)")
            if n == s.id:
                raise ValueError(f"stage {s.id}: next points at itself")
        if isinstance(s.next, list) and (END in s.next or len(set(s.next)) != len(s.next)):
            raise ValueError(f"stage {s.id}: a fan-out lists distinct stages, not `end`")
    if cfg.loop.start and cfg.loop.start not in ids:
        raise ValueError(f"loop.start {cfg.loop.start!r} is not a stage")
    if sum(s.role == "integrator" for s in stages) > 1:
        raise ValueError("at most one integrate agent: it writes the job's decision.md")
    # Order rules apply to the path a job actually takes, not to the order of the list.
    order = cfg.loop.order() if stages else []
    for s in stages:
        if s.on_fail:
            if s.on_fail not in ids:
                raise ValueError(f"stage {s.id}: on_fail {s.on_fail!r} is not a stage")
            if s.id in order and (s.on_fail not in order or order.index(s.on_fail) >= order.index(s.id)):
                raise ValueError(f"stage {s.id}: on_fail must point to an earlier stage")
    bids = {s.id for s in stages if s.role == "builder"}
    keys = [g.key for g in cfg.gate.steps]
    for s in stages:
        if s.role == "reviewer":
            if s.builder and s.builder not in bids:
                raise ValueError(f"reviewer {s.id}: builder {s.builder!r} is not a builder stage")
            for pat in s.diff_patterns + [sp.pattern for sp in s.scoped_patterns]:
                re.compile(pat)
        if s.kind == "gate":
            unknown = set(s.steps) - set(keys) - set(BUILTIN_GUARDRAILS)
            if unknown:
                raise ValueError(f"gate {s.id}: unknown steps {sorted(unknown)}")
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate gate step keys")
    if {"secrets", "clean-tree", "dirty", "passed", "failed", "coverage", "result"} & set(keys):
        raise ValueError("gate step keys secrets, clean-tree, dirty, passed, failed, coverage and result are built in")
    for g in cfg.gate.steps:
        if g.builder and g.builder not in bids:
            raise ValueError(f"gate step {g.key}: builder {g.builder!r} is not a builder stage")
        missing = set(g.requires) - set(keys)
        if missing:
            raise ValueError(f"gate step {g.key}: requires unknown steps {sorted(missing)}")


def warnings(cfg: FactoryConfig) -> list[str]:
    """Allowed but worth a second look; the studio and `factory validate` show these."""
    out = []
    order = cfg.loop.order()
    off_path = [s.id for s in cfg.loop.stages if s.id not in order]
    if off_path:
        out.append(f"stages not connected to the loop never run: {', '.join(off_path)}")
    path = cfg.loop.ordered()
    kinds = [s.kind for s in path]
    if cfg.delivery.mode == "direct" and not cfg.delivery.require_approval:
        out.append("delivery.mode=direct without require_approval puts unreviewed-by-a-human commits on "
                   f"{cfg.repo.default_base} automatically.")
    if "approval" not in kinds and not cfg.delivery.require_approval and cfg.delivery.mode != "none":
        out.append("the loop has no human approval at all and delivers automatically.")
    if cfg.delivery.mode in ("push", "pr", "direct") and cfg.connections.git.provider == "none" \
            and cfg.delivery.mode == "pr":
        out.append("delivery.mode=pr needs connections.git.provider github, gitlab or bitbucket.")
    if "gate" not in kinds:
        out.append("no gate stage: nothing deterministic checks the branch.")
    if not any(s.role == "reviewer" for s in path):
        out.append("no review agent: no agent reviews the code.")
    if not any(s.role == "builder" for s in path):
        out.append("no build agent: gate failures and review findings have nowhere to go back to.")
    for i, s in enumerate(path):
        if (s.kind == "gate" or s.role == "reviewer") and not s.on_fail and not any(
                b.role == "builder" for b in path[:i]):
            out.append(f"{s.id}: no builder before it, so a failure stops the job for a human.")
    for g in cfg.loop.groups():
        if {"builder", "reviewer"} <= {s.role for s in g}:
            out.append(f"{', '.join(s.id for s in g)} run at the same time, builders and reviewers together; "
                       "a reviewer would review code that is still being written.")
    t = cfg.connections.tickets
    if t.provider == "jira" and t.mode == "mcp" and cfg.runtime.kind != "claude-code":
        out.append("tickets.mode=mcp only works in the interactive claude-code runtime; headless runs use REST.")
    if t.write_back in ("comment", "transition") and t.mode == "mcp":
        out.append("Jira write-back runs from the delivery step over REST; set tickets.mode=rest with a token.")
    if cfg.runtime.kind == "codex-cli":
        out.append("codex-cli has no tool hooks: the factory guard cannot vet each command. The Codex sandbox "
                   f"({cfg.runtime.codex_cli.sandbox}) is the boundary.")
    return out
