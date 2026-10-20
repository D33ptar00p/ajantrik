"""The factory.yaml schema.

One file defines a factory: the repo, the runtime that drives the agents, the loop (stages, rounds,
approvals), builders, reviewers and their routing, the gate, connections (git host, tickets) and how
a finished job is delivered. Agents and skills are markdown files next to it (see project.py).

`render` writes the validated model, with every default filled in, to `.factory/factory.json`. The
stdlib-only runtime in ajantrik/core (hooks, gate, engine, dashboard) reads that file, so defaults live
here and only here.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ajantrik.core.graph import END, run_order

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
StageKind = Literal["agent", "approval", "builders", "gate", "reviewers", "integrate", "delivery"]


class Pos(_Model):
    x: float
    y: float


class Stage(_Model):
    id: str = Field(description="Stage id, also the job.json `stage` value (approval stages: awaiting-<id>).")
    kind: StageKind
    title: str = ""
    next: str | None = Field(
        None, description="Success edge: the stage that follows. Unset: the next one in the list. `end`: done.")
    pos: Pos | None = Field(None, description="Where the loop designer draws this stage. Layout only.")
    agent: str | None = Field(None, description="kind agent/integrate: the agent that runs.")
    output: str | None = Field(None, description="kind agent: file the agent writes in the job folder.")
    prompt: str = Field("", description="Extra instructions appended to the agent prompt for this stage.")
    read_builders: bool = Field(False, description="Read the `Builders:` line of the output into job.builders.")
    on_fail: str | None = Field(None, description="gate/reviewers: stage to return to (default: the builders stage).")
    note: str = Field("", description="approval: what the card says while waiting.")
    rerun_on_rework: Literal["always", "scope", "never"] = Field(
        "never", description="agent stages before the first approval: re-run on rework (scope = spec changes only)."
    )

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID.match(v):
            raise ValueError("stage ids are lower-case kebab: [a-z][a-z0-9-]*")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Stage:
        if self.kind in ("agent", "integrate") and not self.agent:
            raise ValueError(f"stage {self.id}: kind {self.kind} needs `agent`")
        if self.kind == "agent" and not self.output:
            self.output = f"{self.id}.md"
        if self.kind == "integrate" and not self.output:
            self.output = "decision.md"
        return self


def default_stages() -> list[Stage]:
    return [
        Stage(id="spec", kind="agent", agent="spec-writer", output="spec.md", title="Spec", rerun_on_rework="scope"),
        Stage(id="design", kind="agent", agent="architect", output="design.md", title="Design", read_builders=True,
              rerun_on_rework="always"),
        Stage(id="approval", kind="approval", title="Approve", note="approve the spec and design"),
        Stage(id="tests", kind="agent", agent="test-author", output="tests.md", title="Tests"),
        Stage(id="build", kind="builders", title="Build"),
        Stage(id="gate", kind="gate", title="Gate"),
        Stage(id="review", kind="reviewers", title="Review"),
        Stage(id="integrate", kind="integrate", agent="integrator", title="Integrate"),
        Stage(id="deliver", kind="delivery", title="Deliver"),
    ]


class Loop(_Model):
    max_rounds: int = Field(3, ge=1, le=10, description="A round is one gate run plus, if it passes, one review.")
    start: str | None = Field(None, description="The first stage a job enters. Default: the first in the list.")
    stages: list[Stage] = Field(default_factory=default_stages)

    def order(self) -> list[str]:
        """Stage ids in the order a job visits them (the path of success edges from the start)."""
        return run_order(self.stages, self.start)

    def ordered(self) -> list[Stage]:
        by_id = {s.id: s for s in self.stages}
        return [by_id[i] for i in self.order()]


# ---- builders and reviewers -----------------------------------------------------------------------
class Builder(_Model):
    id: str = Field(description="Short id: builder agent writes build-<id>.md; named builder-<id>-<job>.")
    agent: str | None = Field(None, description="Agent file; default builder-<id>.")
    area_globs: list[str] = Field(default_factory=list, description="Paths this builder owns, for routing findings.")
    description: str = ""

    @model_validator(mode="after")
    def _agent(self) -> Builder:
        self.agent = self.agent or f"builder-{self.id}"
        return self


class ScopedPattern(_Model):
    paths: list[str] = Field(description="Only added lines in files matching these globs count.")
    pattern: str = Field(description="Python regex searched in each added line.")


class MetricRule(_Model):
    key: str = Field(description="gate.json key, e.g. data_mb_delta.")
    gt: float


class RoutedReviewer(_Model):
    id: str
    agent: str | None = Field(None, description="Agent file; default reviewer-<id>.")
    chip: str | None = Field(None, description="Dashboard chip label; default the id.")
    path_globs: list[str] = Field(default_factory=list, description="Run when any changed path matches.")
    exclude_globs: list[str] = Field(default_factory=list, description="Changed paths that never trigger it (tests).")
    diff_patterns: list[str] = Field(default_factory=list, description="Run when any added line matches (regex).")
    scoped_patterns: list[ScopedPattern] = Field(default_factory=list)
    metrics: list[MetricRule] = Field(default_factory=list, description="Run when a gate metric exceeds a value.")
    builder: str | None = Field(None, description="Builder that gets this reviewer's findings (default: by path).")

    @model_validator(mode="after")
    def _defaults(self) -> RoutedReviewer:
        self.agent = self.agent or f"reviewer-{self.id}"
        self.chip = self.chip or self.id
        return self


class AlwaysReviewer(_Model):
    id: str
    agent: str | None = None
    chip: str | None = None

    @model_validator(mode="after")
    def _defaults(self) -> AlwaysReviewer:
        self.agent = self.agent or f"reviewer-{self.id}"
        self.chip = self.chip or {"security": "sec"}.get(self.id, self.id)
        return self


class Reviewers(_Model):
    always: list[AlwaysReviewer] = Field(
        default_factory=lambda: [AlwaysReviewer(id="code"), AlwaysReviewer(id="security")]
    )
    routed: list[RoutedReviewer] = Field(default_factory=list)

    @field_validator("always", mode="before")
    @classmethod
    def _strings(cls, v: list) -> list:
        return [{"id": x} if isinstance(x, str) else x for x in v]


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


class Gate(_Model):
    setup: list[Check] = Field(default_factory=list, description="Checks before any step (venv, node_modules...).")
    steps: list[GateStep] = Field(default_factory=list)
    secrets: bool = Field(True, description="Scan every commit on the branch for secrets.")
    clean_tree: bool = Field(True, description="Fail when the worktree has uncommitted changes afterwards.")
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
    build_label: str = Field("", description="Under the Build column, e.g. 'py ∥ qml'. Default: builder ids.")
    job_files: list[str] = Field(default_factory=list, description="Drawer file order. Default: from the loop.")
    skip_dirs: list[str] = Field(default_factory=lambda: ["mockup", "coverage", "e2e"])


class FactoryConfig(_Model):
    version: Literal[1] = 1
    repo: Repo
    runtime: Runtime = Field(default_factory=Runtime)
    loop: Loop = Field(default_factory=Loop)
    builders: list[Builder] = Field(default_factory=lambda: [Builder(id="main")])
    reviewers: Reviewers = Field(default_factory=Reviewers)
    gate: Gate = Field(default_factory=Gate)
    preflight: list[Preflight] = Field(default_factory=list)
    worktree: list[WorktreeStep] = Field(default_factory=list)
    conventions: Conventions = Field(default_factory=Conventions)
    connections: Connections = Field(default_factory=Connections)
    delivery: Delivery = Field(default_factory=Delivery)
    security: Security = Field(default_factory=Security)
    ui: Ui = Field(default_factory=Ui)

    @model_validator(mode="after")
    def _loop(self) -> FactoryConfig:
        validate_loop(self)
        return self

    # convenient lookups -----------------------------------------------------------------------
    def stage(self, sid: str) -> Stage | None:
        return next((s for s in self.loop.stages if s.id == sid), None)

    def agent_names(self) -> set[str]:
        """Every agent the config refers to."""
        names = {s.agent for s in self.loop.stages if s.agent}
        if any(s.kind == "builders" for s in self.loop.stages):
            names |= {b.agent for b in self.builders if b.agent}
        if any(s.kind == "reviewers" for s in self.loop.stages):
            names |= {r.agent for r in self.reviewers.always if r.agent}
            names |= {r.agent for r in self.reviewers.routed if r.agent}
        return names


def validate_loop(cfg: FactoryConfig) -> None:
    stages = cfg.loop.stages
    ids = [s.id for s in stages]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate stage ids: {sorted(dupes)}")
    reserved = {"ready", "needs-human", "abandoned", "delivered", END}
    if reserved & set(ids):
        raise ValueError(f"stage ids {sorted(reserved & set(ids))} are reserved job states")
    for kind in ("builders", "gate", "reviewers", "integrate", "delivery"):
        if sum(s.kind == kind for s in stages) > 1:
            raise ValueError(f"at most one {kind} stage")
    for s in stages:
        if s.next and s.next != END and s.next not in ids:
            raise ValueError(f"stage {s.id}: next {s.next!r} is not a stage (or `end`)")
        if s.next == s.id:
            raise ValueError(f"stage {s.id}: next points at itself")
    if cfg.loop.start and cfg.loop.start not in ids:
        raise ValueError(f"loop.start {cfg.loop.start!r} is not a stage")
    # Order rules apply to the path a job actually takes, not to the order of the list.
    order = cfg.loop.order() if stages else []
    kinds = {s.id: s.kind for s in stages}
    pos = {kinds[sid]: i for i, sid in enumerate(order)}
    if ("gate" in pos or "reviewers" in pos) and "builders" not in pos:
        raise ValueError("gate and reviewers need a builders stage before them to send findings back to")
    for k in ("gate", "reviewers"):
        if k in pos and pos[k] < pos["builders"]:
            raise ValueError(f"the {k} stage must come after the builders stage")
    if "delivery" in pos and pos["delivery"] != len(order) - 1:
        raise ValueError("the delivery stage must be last: nothing can follow it")
    for s in stages:
        if s.on_fail:
            if s.on_fail not in ids:
                raise ValueError(f"stage {s.id}: on_fail {s.on_fail!r} is not a stage")
            if s.id in order and (s.on_fail not in order or order.index(s.on_fail) >= order.index(s.id)):
                raise ValueError(f"stage {s.id}: on_fail must point to an earlier stage")
    bids = [b.id for b in cfg.builders]
    if len(set(bids)) != len(bids):
        raise ValueError("duplicate builder ids")
    if "builders" in pos and not bids:
        raise ValueError("a builders stage needs at least one builder")
    rids = [r.id for r in cfg.reviewers.always] + [r.id for r in cfg.reviewers.routed]
    if len(set(rids)) != len(rids):
        raise ValueError("duplicate reviewer ids")
    for r in cfg.reviewers.routed:
        if r.builder and r.builder not in bids:
            raise ValueError(f"reviewer {r.id}: builder {r.builder!r} is not a builder id")
        for p in r.diff_patterns + [sp.pattern for sp in r.scoped_patterns]:
            re.compile(p)
    keys = [g.key for g in cfg.gate.steps]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate gate step keys")
    if {"secrets", "dirty", "passed", "failed", "coverage", "result"} & set(keys):
        raise ValueError("gate step keys secrets, dirty, passed, failed, coverage and result are built in")
    for g in cfg.gate.steps:
        if g.builder and g.builder not in bids:
            raise ValueError(f"gate step {g.key}: builder {g.builder!r} is not a builder id")
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
    kinds = [s.kind for s in cfg.loop.ordered()]
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
    if "reviewers" not in kinds:
        out.append("no reviewers stage: no agent reviews the code.")
    t = cfg.connections.tickets
    if t.provider == "jira" and t.mode == "mcp" and cfg.runtime.kind != "claude-code":
        out.append("tickets.mode=mcp only works in the interactive claude-code runtime; headless runs use REST.")
    if t.write_back in ("comment", "transition") and t.mode == "mcp":
        out.append("Jira write-back runs from the delivery step over REST; set tickets.mode=rest with a token.")
    if cfg.runtime.kind == "codex-cli":
        out.append("codex-cli has no tool hooks: the factory guard cannot vet each command. The Codex sandbox "
                   f"({cfg.runtime.codex_cli.sandbox}) is the boundary.")
    return out
