---
name: run-checks
description: Run the repository's own lint, type and test commands instead of reasoning about whether they would pass.
---

# Run the repo's checks

The repository's test and lint commands are the definition of done that a
human will apply. Run them, rather than reasoning about whether they would pass.

## Rules

- Use the commands from the repo's context files (`CLAUDE.md` / `AGENTS.md`),
  its task runner or its CI workflow. A command you invent probably is not
  installed.
- Read the first failure, not the last. Later failures are usually the first
  one repeating.
- A lint failure is a real failure. Fix it rather than leaving it for review.
- Never install packages to make a check run; a missing tool is a BLOCKED build.
