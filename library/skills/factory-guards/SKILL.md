---
name: factory-guards
description: What the factory guard blocks for every agent and how to report a block honestly instead of routing around it.
---

# Factory guards

Every tool call an agent makes in the factory is checked by a guard before it
runs. It blocks:

- reading secret files (`.env*`, keys, cloud and git credentials) or dumping
  the environment, and expanding secret variables into commands;
- sending data off the machine (`ssh`, `scp`, `nc`, uploads and POSTs with
  `curl`/`wget`) and piping downloads into a shell;
- `git push`, `--no-verify`, global git config, remote changes, and git
  commands that change the main checkout;
- installing packages (`pip`, `npm install`, `uv add`...) and running `npx`
  packages that are not already in the repo;
- writes outside the job worktree, the job folder and temp folders, and any
  change to the factory's own files (`.claude/`, `.factory/` except job state);
- writes to external services (ticket systems, pages, designs, GitHub);
- secrets in the content of files you write.

## Rules

- A `factory guard: blocked: ...` result is final. Don't retry the same thing
  another way (a different command, a script, a heredoc).
- If the job can't be done without the blocked action, stop and report it in
  your output file (BLOCKED, ESCALATE or a finding), quoting the message.
- A dependency the job needs, a secret it needs, or a change to CI or the
  factory itself is a decision for a human, never for an agent.
