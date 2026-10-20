---
name: conventional-commits
description: Write commit messages in the repository's own style and the factory's commit format, imperative, saying what and why.
---

# Commit messages

A message in the repository's own style reads like the commits around it.

## Rules

- Follow the commit format from your prompt (`Commit format: ...`). When it
  names a type, use `feat`, `fix`, `test`, `refactor`, `chore`, `docs`, `perf`,
  `style`, `build` or `ci`.
- One subject line, imperative mood: "add the retry policy", not "added".
- Say what changed and why, not which files you touched; the diff already says
  that.
- Check `git log --oneline -10` for the repo's habits (ticket prefix, casing)
  and match them.
