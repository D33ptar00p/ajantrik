# Security policy

Ajantrik runs AI agents against your code, so security reports are taken seriously.

## Reporting a vulnerability

**Do not open a public issue.** Report privately through GitHub:
[Security → Report a vulnerability](https://github.com/D33ptar00p/ajantrik/security/advisories/new).

Please include:

- the affected version or commit,
- the runtime (`claude-code`, `claude-cli`, `codex-cli`, `api`) and OS,
- steps to reproduce, and the impact you expect.

You should get an acknowledgement within a few days. Once a fix is ready it is released and the
advisory published, crediting you unless you ask otherwise.

## Supported versions

Ajantrik is pre-1.0. Only the latest release on `main` receives security fixes.

## Scope

In scope: anything that lets an agent get around the guard or the sandbox, for example writing
outside its worktree, reaching hosts outside `network.allowed_domains`, reading credentials,
approving or delivering a job, or pushing to a remote. Also the studio (local token, 127.0.0.1
binding) and credential handling.

How the guard and sandbox are meant to work: [docs/security.md](docs/security.md).
