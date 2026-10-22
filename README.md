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
Plan:      spec-writer → architect → ⏸ approve
Build:     test-author → builder-main (more builders: a fan-out, they run in parallel)
Review:    gate ⇉ reviewer-code ∥ reviewer-security ∥ … (the gate fans out to the reviewers)
Integrate: integrator → deliver (none | push | pr | direct, optionally after ⏸ approval)
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
(Ctrl+C stops it). Run inside a
project repo, it opens that repo's factory. Run anywhere else, it lists every registered project
(`factory projects add <path>`). For the jobs board alone, without the editor, run `.factory/ui.sh`.

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

## Layout: one config, several editions

```
ajantrik/
  kit/            the installed `factory` CLI: schema (factory.yaml), project files, render, doctor
    templates/    the /factory orchestrator skill, dashboard, ui.sh
  core/           the runtime-neutral engine, stdlib only; copied into each repo as .factory/lib/ajantrik_core
    engine.py     the loop as data: step / advance / gate / approve / rework / deliver
    gate.py routing.py delivery.py jobs.py
    connections/  git_host (gh, glab, Bitbucket REST), tickets (Jira REST, backlog), secrets (env:/keyring:)
    security/     guard.py (hooks + library), policy.py, secret_scan.sh: lifted from the Move factories
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

The engine (`ajantrik/core/engine.py`) reads the stage list and answers one question: *what next?*

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

Phases (`loop.phases`) are only board columns, in order: Plan, Build, Review and Integrate by default,
and you can add your own. An agent block goes in any phase without changing what it does; a gate
goes in Review unless you put it elsewhere; approvals have no phase. What runs in parallel comes
from the arrows: a block whose success edge fans out (`next: [a, b]`, or a second arrow drawn in the
studio) starts `a` and `b` at the same time as one step. Each branch is a single agent, and all
branches must lead on to the same block (or the same fan-out), where the path joins again. Without a
fan-out, blocks run one after the other. Delivery
is not a block: after the last one the job is delivered by `delivery.mode` (`none`, `push`, `pr` or
`direct` with `ff-only` or `squash`), optionally after an approval.

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
