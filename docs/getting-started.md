# Getting started

Install Ajantrik, set up a factory in a repository and run your first job.

## Install and initialise

```bash
uv tool install ".[api]"            # or: uv sync --extra api && uv run factory ...
cd /path/to/your/repo
factory presets                     # default
factory init                        # writes .factory/config/ and renders .claude/ + .factory/
factory studio                      # edit loop, agents, skills, guardrails, connections, delivery, runtime
factory doctor                      # config, render drift, git, tickets and runtime checks
```

## Starting the studio

```bash
factory studio                      # or, from a clone: .venv/bin/factory studio / uv run factory studio
factory studio --port 8765          # pick the port
factory studio --no-browser         # don't open a browser; open the printed URL yourself
```

The studio listens on 127.0.0.1 only. Each start mints a new token, carried by the URL it prints
(Ctrl+C stops it). Run inside a project repo, it opens that repo's factory. Run anywhere else, or
with `--all`, it opens the Projects page, listing every registered project (see [Projects](projects-and-backlog.md#projects)).
For the jobs board alone, without the editor, run `.factory/ui.sh`.

A project's tabs are Board, Backlog, Loop, Agents, Skills, Guardrails, Connections, Delivery,
Runtime, Sandbox and Raw YAML. What the Board's actions do depends on the runtime. With a headless runtime
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

Next: [CLI reference](cli.md) · [How the loop works](loop.md)

---

[← Docs index](README.md) · [Project README](../README.md)
