---
name: establish-test-setup
description: If the repository has no way to run tests, set the minimal one up in the repo's own style, with no new dependencies.
---

# Establish a test setup

A repository that cannot run a test cannot prove anything, and a job in one is
unverifiable however good the code is. If there is no way to run tests yet,
establishing one is part of the first job, not a separate project.

What that means is the repository's business, not a house style. Read what is
already there and follow it. A project with a `pyproject.toml` has chosen its
runner; a project with `unittest.TestCase` files has chosen a different one; an
empty repository has chosen nothing and the cheapest correct answer is usually
the language's standard library, because it needs no install.

## Rules

- Look before you choose. If any test already exists, write yours in the same
  style and put it where the others are. A second style in one repository
  means one of them stops being collected.
- If nothing exists, prefer what runs with no installation. The factory cannot
  install packages.
- Put tests where the language expects them: `tests/` for Python, next to the
  source for Go, wherever the existing project already puts them.
- Say what you set up in your output file, so later jobs follow it rather than
  inventing a second convention.
- Do not add a dependency to make a test run. If the only way to test the work
  needs something that is not installed, report BLOCKED.
