# Security model

Two layers: the guard (rules on every tool call) and the sandbox (the operating system).

To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Guard

The guard enforces these rules:

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
- `codex-cli` has no tool hooks: the whole `codex exec` process runs in the sandbox (below) and inside
  it the Codex sandbox (`workspace-write`, worktree and job folder, no network), and `factory validate`
  says so.

## Sandbox

The guard is rules; the sandbox is the operating system. With `sandbox.enabled` (the default), feature
development and testing happen inside Anthropic's sandbox-runtime (`srt`: bubblewrap on Linux, Seatbelt
on macOS, alpha on Windows):

| What | How |
|---|---|
| agents' commands, `claude-code` / `claude-cli` | render writes the `sandbox` block of `.claude/settings.json`; Claude Code runs Bash in it. `python3 .factory/factory.py *` is excluded: it sandboxes setup and the gate itself, and delivery needs the git host |
| agents' commands, `api` | the Bash tool runs `srt --settings … bash -c <cmd>` |
| agents' file edits | Read, Edit and Write follow the same `deny_read` / `deny_write` rules (Claude Code: rendered permission rules; `api`: the factory's own tools) |
| worktree setup and every gate step (`sandbox.apply.gate`) | `srt` around each `bash -c` |
| `codex-cli` (`sandbox.apply.codex`) | `srt` around the whole `codex exec`, with the OpenAI hosts allowed. On macOS Seatbelt cannot nest, so there srt is the boundary and Codex runs with `danger-full-access` |

A job's commands write only its own worktree, its job folder, the temp dir and `filesystem.allow_write`,
and reach only `network.allowed_domains` (by default the PyPI and npm registries). Every srt option is in
`factory.yaml` under `sandbox:` and in the studio's Sandbox tab. Each run's srt settings go to
`.factory/.sandbox/`, where no sandboxed command can write.

The sandbox **fails closed**: when it cannot start, agents and the gate refuse to run and the job goes
to needs-human (`fail_if_unavailable: false` runs unsandboxed with a warning instead). Install it and
check with `factory doctor --only sandbox`:

```bash
npm i -g @anthropic-ai/sandbox-runtime
sudo apt install bubblewrap socat ripgrep      # Linux; macOS: brew install ripgrep
```

On Ubuntu 24.04+ bubblewrap may also need `kernel.apparmor_restrict_unprivileged_userns=0` or an
AppArmor profile. Configs written before the sandbox existed get it on, with these defaults.

## Development

---

[← Docs index](README.md) · [Project README](../README.md)
