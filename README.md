# Ajantrik

**Software Factory**

A configurable version of the Move AI software factories (mocap-desktop, accounts-portal, omnigrid).
One `factory.yaml` plus markdown agents and skills defines the whole factory:

- the **loop**: agent, approval and gate blocks, the phase (board column) each agent and gate is
  in, rounds and where failures go back to
- **agents and skills**: edit, add, clone them from the library. Every worker in the loop is an
  agent, and the agent says what it does (writes a file, builds, reviews, integrates)
- **guardrails**: deterministic checks (secret and PII scans, topic/word and regex rules, your
  tests and linters); a gate block can sit in any phase and run any of them
- **connections**: the git host (GitHub, GitLab, Bitbucket) and Jira
- **delivery**: what happens to a finished branch (nothing, push, PR, or straight onto the base),
  with or without a human approval first
- the **runtime** that runs the agents: interactive Claude Code, the Claude Code CLI, the Codex
  CLI, or any LLM API (Anthropic, OpenAI, Bedrock, a local Ollama, a LiteLLM proxy)

You edit all of it in a local web **studio** or by hand.

```
Plan:       spec-writer → architect → ⏸ approve
Build:      test-author → builder-main (more builders: a fan-out, they run in parallel)
Guardrails: gate (secret scan, clean tree and the guardrails it picks)
Review:     ⇉ reviewer-code ∥ reviewer-security ∥ … (the gate fans out to the reviewers)
Integrate:  integrator → deliver (none | push | pr | direct, optionally after ⏸ approval)
gate fail / review CHANGES → back to the builders, max N rounds, then needs-human
```

Every block in that loop can be changed, moved to another phase or removed, a phase can hold as
many agents as you like, and you can add phases. The board shows the phases in order, then Done, then
Needs human, where every approval and anything that needs a person waits.

## Quick start

```bash
uv tool install ".[api]"            # or: uv sync --extra api && uv run factory ...
cd /path/to/your/repo
factory presets                     # blank, python-qt, react-vite, python-ts-data
factory init --preset blank         # writes .factory/config/ and renders .claude/ + .factory/
factory studio                      # edit loop, agents, skills, guardrails, connections, delivery, runtime
factory doctor                      # config, render drift, git, tickets and runtime checks
```

### Starting the studio

```bash
factory studio                      # or, from a clone: .venv/bin/factory studio / uv run factory studio
factory studio --port 8765          # pick the port
factory studio --no-browser         # don't open a browser; open the printed URL yourself
```

The studio listens on 127.0.0.1 only. Each start mints a new token, carried by the URL it prints
(Ctrl+C stops it). Run inside a project repo, it opens that repo's factory. Run anywhere else, or
with `--all`, it opens the Projects page, listing every registered project (see [Projects](#projects)).
For the jobs board alone, without the editor, run `.factory/ui.sh`.

A project's tabs are Board, Backlog, Loop, Agents, Skills, Guardrails, Connections, Delivery,
Runtime and Raw YAML. What the Board's actions do depends on the runtime. With a headless runtime
(`claude-cli`, `codex-cli`, `api`) the Board can approve, continue and deliver jobs. With
`claude-code` it copies the `/factory go|deliver|rework` command for you to run, because the
interactive session is the orchestrator.

Then run a job:

| Runtime (`runtime.kind`) | How you run it |
|---|---|
| `claude-code` | In Claude Code in the repo: `/factory "Add rename to projects"`, then `/factory go <job>` |
| `claude-cli` | `factory run "Add rename to projects"`, then `factory go <job>` at each approval |
| `codex-cli` | same as above, through `codex exec` |
| `api` | same as above, through LiteLLM: `provider: anthropic \| openai \| bedrock \| ollama \| litellm \| openai-compatible` |

Jobs started in one runtime can be finished in another: they share `.factory/jobs/<id>/`.
The dashboard (`.factory/ui.sh`, or the studio's Board tab) shows every job's stage, gate and
review results.

### Commands

```bash
factory init [--preset NAME] [--name REPO] [--force]   # create .factory/config/ from a preset, then render
factory presets                                        # list presets
factory validate                                       # check the config (errors and warnings)
factory render [--check]                               # write .claude/ and .factory/ from the config
factory studio [--port N] [--no-browser] [--all]       # the web studio
factory projects                                       # list projects
factory projects add <path> [--runtime R] [--preset P] [--name N]   # register a repo, setting up its factory
factory projects remove <id>                           # forget a project (its files stay)
factory doctor [--only git|tickets|runtime]            # check connections and the runtime
factory run "<request>" [--ticket K] [--base B] [--wait]   # headless: create a job and drive it
factory go <job> [--wait]                              # headless: approve the waiting stage and continue
factory resume <job> [--wait]                          # headless: continue a job
factory next [--wait] [--create-only] [--items 0,2]    # claim backlog items and run them (see Backlog)
factory new|step|advance|refresh|gate|approve|rework|deliver|abandon|status|sync|route|ticket
```

The last line is the job commands: they are the same as `python3 .factory/factory.py <cmd>`
(`python3 .factory/factory.py --help` lists them). `--json` anywhere on the line prints
machine-readable output, which the studio relies on. `--root <path>` points at a repo other than
the current one. `--wait` waits at approvals instead of stopping.

## Layout: one config, several editions

```
ajantrik/
  kit/            the installed `factory` CLI: schema (factory.yaml), project files, render, doctor, projects
    templates/    the /factory orchestrator skill, dashboard, ui.sh
    workspace.py  the projects registry ($AJANTRIK_HOME/projects.json)
  core/           the runtime-neutral engine, stdlib only; copied into each repo as .factory/lib/ajantrik_core
    engine.py     the loop as data: step / advance / gate / approve / rework / deliver
    graph.py      the loop as a graph: next, fan-out groups, loop.start, and what an agent does (role)
    gate.py routing.py delivery.py jobs.py
    connections/  git_host (gh, glab, Bitbucket REST), tickets (Jira REST, backlog), secrets (env:/keyring:)
    security/     guard.py (hooks + library), policy.py, secret_scan.sh: lifted from the Move factories;
                  pii_scan.py, content_scan.py: the PII and topic/regex guardrails
  editions/
    claude_code/  edition 1: interactive Claude Code; render writes .claude/agents, skills, settings, /factory
    headless/     edition 2: `factory run` drives the loop through a backend
      backends/   cli_agents.py (claude -p, codex exec), llm_api.py (LiteLLM tool loop), tools.py (guarded tools)
  studio/         local web UI + API (127.0.0.1, token, no build step)
library/          generic agents and skills, the starting point for new ones
presets/          blank, python-qt (mocap-desktop), react-vite (accounts-portal), python-ts-data (omnigrid)
```

In a configured repo:

```
.factory/config/factory.yaml           source of truth (studio edits it; history in config/.history/)
.factory/config/agents/*.md            agents: frontmatter (tools, model, skills) + instructions
.factory/config/skills/<name>/SKILL.md skills
.factory/factory.json                  rendered, with every default: read by hooks, gate, engine, dashboard
.factory/factory.py                    the job commands, stdlib only: python3 .factory/factory.py --help
.claude/agents, .claude/skills, .claude/settings.json   rendered for Claude Code
.factory/jobs/<id>/                    job state (gitignored); worktrees in ../<repo>-factory/<id>/
```

Only `.factory/config/` (and `.factory/harness/`, `.factory/security/allow.txt`, `.factory/backlog.md`)
is yours to edit. Everything else is regenerated by `factory render`, which also reports files that
were edited by hand and removes files for agents you deleted.

## How the loop works

The engine (`ajantrik/core/engine.py`) reads the stages as a graph (`ajantrik/core/graph.py`) and
answers one question: *what next?*

- `step <job>` returns one action: run these agents (in parallel or not, fresh or resumed), run the
  gate, wait for an approval, deliver, or stop.
- After the agents of a stage finish, `advance <job>` checks what they wrote, using the same
  marker lines as the hand-built factories (`STATUS: READY|BLOCKED`, `VERDICT: PASS|CHANGES`,
  `READY|ESCALATE`, `Builders:`). Then it moves the job on, back a round, or to needs-human.

The interactive `/factory` skill and the headless runner both drive this same API, so the loop is
configuration rather than prose. A loop has three kinds of block:

| kind | does |
|---|---|
| `agent` | runs one agent; its `role` says what it writes (below) |
| `approval` | the job waits at `awaiting-<id>` until a human approves (on the board: Needs human) |
| `gate` | the guardrails it names (`steps`: `gate.steps` keys and the built-in `secrets` and `clean-tree`; empty = all) |

| agent role | writes | and then |
|---|---|---|
| `plain` | its `output` file (spec, design, tests, or anything you add) | `BLOCKED` on the first line stops the job |
| `builder` | code commits and `build-<id>.md` | the ones the design picked (`Builders:` line) run; later rounds resume them with the findings |
| `reviewer` | `round-<N>/review-<id>.md` with `VERDICT: PASS\|CHANGES` | runs always, or only when its path globs, diff patterns or gate metrics match |
| `integrator` | `decision.md` and the PR text (one at most) | refuse-path check before it, outgoing secret scan after it |

The role comes from the agent: `builder-*` agents build, `reviewer-*` agents review, `integrator`
integrates and every other agent writes a file. A block's `role` overrides that.

Phases (`loop.phases`) are only board columns, in order: Plan, Build, Guardrails, Review and
Integrate by default, and you can add your own. An agent block goes in any phase without changing
what it does; a gate goes in Guardrails (else Review) unless you put it elsewhere; approvals have no
phase. What runs in parallel comes
from the arrows: a block whose success edge fans out (`next: [a, b]`, or a second arrow drawn in the
studio) starts `a` and `b` at the same time as one step. Each branch is a single agent, and all
branches must lead on to the same block (or the same fan-out), where the path joins again. Without a
fan-out, blocks run one after the other. Delivery
shows in the studio as a single Deliver block that always runs after the last block; it is the same
`delivery` settings as the Deliver tab: after the last block the job is delivered by `delivery.mode`
(`none`, `push`, `pr` or `direct` with `ff-only` or `squash`), optionally after an approval.

The blocks form a graph:

- Each block has an optional `next`: a block id, `end` (the job is ready), or a list of ids (a
  fan-out, above).
- Without `next` a block continues to the one after it in the list, so a plain ordered list works
  as it always did.
- `loop.start` is the first block; it defaults to the first in the list.
- The run order is the path of `next` edges from `loop.start`. Blocks off that path never run.
- Gates and reviewer agents also have an `on_fail` edge. It must point to an earlier block and
  defaults to the nearest builder before it (below).

### Guardrails

A guardrail is a bash command run in the job's worktree (`gate.steps`); it fails the gate with a
non-zero exit. The studio's Guardrails tab shows the command each one runs. Two are built in: the
secret scan (`gate.secrets`: every commit on the branch, plus gitleaks when installed) and the
clean-tree check (`gate.clean_tree`). The library (`library/guardrails.yaml`, "+ From library") adds:

| guardrail | checks the lines the branch adds for |
|---|---|
| `pii` | email addresses, phone numbers, US SSNs, payment cards (Luhn) and IBANs (checksum); placeholders such as example.com are ignored |
| `topics` | named lists of words or phrases (`TOPICS: "codenames: project falcon; banned: …"`), case-insensitive |
| `regex` | your own patterns (`PATTERNS`, one per line), optionally only in `PATHS` |
| `large-files` | added or grown files over `LIMIT_KB` |
| `conflict-markers` | unresolved merge conflict markers |
| `dependency-audit` | known vulnerabilities, with pip-audit or npm audit when a dependency file changes |
| `debug-leftovers` | `breakpoint()`, `pdb.set_trace()`, `console.log()`, `debugger` (report only by default) |

The PII, topic and regex scans write findings as file, line, rule and fingerprint, never the matched
text. To accept a reviewed false positive, add its fingerprint to `.factory/security/allow.txt`.

A failing gate or a reviewer's `CHANGES` sends the job back to its `on_fail` (the default loop sets
it to the builder), or to the nearest builder before it; with no builder before it, the job goes to a human. Only the builders that own
the findings rebuild, when that is known (a reviewer's or guardrail's `builder`). A round opens at
the first gate or reviewer after a build; checks before any builder run in round 0. Once
`max_rounds` is used up, the job goes to needs-human.

Configs written before phases (`version: 1`, with `builders:` and `reviewers:` teams) still load:
each builder becomes a build agent and each reviewer a review agent, failures go back to the first
builder explicitly, and the next save writes version 2.

## Projects

A project is one git repository with its own factory. The registry only remembers where they are:
`$AJANTRIK_HOME/projects.json` (default `~/.ajantrik`).

```bash
factory projects                          # list the registered repos
factory projects add ~/code/my-repo       # register a repo; if it has no factory, sets one up from --preset and --runtime
factory projects remove <id>              # forget a project: the repo and its .factory/ stay as they are
```

`factory studio` run outside a configured repo, or with `--all`, opens the Projects page.

## Backlog

`.factory/backlog.md` is the queue of work for `factory next`. Items go under `## Queue`; add them
by hand or with `factory sync`:

```
- [ ] GEN-781 · base: feature/GEN-750_Projects · Add project rename      (ticket)
- [ ] base: main · Escape plant names in tooltips                          (free text)
- [!] GEN-795 · epic GEN-802 has no branch on origin · Actor profile export
- [x] GEN-781 · base: ... · Add project rename → 004-GEN-781-project-rename
```

- `- [ ]` is queued: a ticket, or free text as `base: <branch> · <request>`.
- `- [x]` is done, with `→ <job id>`.
- `- [!]` is blocked until a human resolves it.
- `factory sync` fills the file from the ticket system. `factory sync --push` first creates issues
  for queued lines that have no ticket. It writes to the ticket system, so it is a human's command.
- `factory next` claims the first `- [ ]` item and runs it headless. `--create-only` only creates
  the job. `--items 0,2` picks items by their 0-based position instead.
- The studio's Backlog tab shows the same file. When the runtime is headless, ticked queued items
  can be started as one job there (they must share one ticket and one base branch).

## Security

The guard from the hand-built factories is kept and extended:

- It runs as Claude Code hooks for `claude-code` and `claude-cli`.
- It also runs as a library before every tool call in the `api` runtime. There, the Read, Grep,
  Glob, Edit, Write and Bash tools are the factory's own and go through the same rules.
- Agents can never push, merge, write to GitHub or Jira, approve a job or deliver one.
- Approvals are files only `factory approve` writes, and agents cannot write them. The interactive
  orchestrator can approve only right after you typed `/factory go|deliver <job>`.
- Delivery is the only code that writes to a remote. It runs after the outgoing secret scan and,
  when configured, after your approval.
- Credentials are always references (`env:NAME`, `keyring:service/name`), and the schema rejects
  literal values.
- Agent subprocesses get an environment with credentials stripped.
- `codex-cli` has no tool hooks: the Codex sandbox (`workspace-write`, worktree and job folder, no
  network) is the boundary, and `factory validate` says so.

## Development

```bash
uv sync --extra api
uv run pytest            # unit, end-to-end loop (scripted backend), backends (fake CLIs), presets vs originals
uv run ruff check ajantrik tests && uv run mypy
```

The preset tests compare each Move preset with its hand-built factory (agents, models, skills,
settings) when those repos are on the machine.
