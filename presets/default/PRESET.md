# Default factory

The generic factory ajantrik runs on itself: the full loop (spec, design, a
secrets guardrail, approval, tests, one builder, gate, code and security review,
integrate, deliver) with its seven agents (`spec-writer`, `architect`,
`test-author`, `builder-main`, `reviewer-code`, `reviewer-security`,
`integrator`) and five skills (`establish-test-setup`, `read-before-edit`,
`run-checks`, `smallest-change`, `test-first`). Nothing in it is specific to a
language, framework or ticket system.

## Start

1. `factory init` in the repo (or add it as a project in the studio), then open
   the studio (`factory studio`).
2. **Repo**: set the name and the default base branch.
3. **Gate**: add one step per check the repo already runs in CI (tests with
   `junit`, lint, typecheck, build). Out of the box the gate scans for PII,
   merge-conflict markers and secrets, and checks the tree is clean.
4. **Builders**: split the work if the repo has clearly separate areas (for
   example `api` and `web`), each with its `area_globs`. The engine copies the
   library builder for each new id; tailor its instructions.
5. **Skills**: write one skill per set of repo rules agents keep needing
   (conventions, test patterns, how to run the app) and attach it to the agents
   that need it.
6. **Reviewers**: add routed specialists (`path_globs`, `diff_patterns`) for
   risky areas such as billing, migrations or deploy config.
7. **Connections and delivery**: point at the git host and ticket system, and
   choose how a finished job leaves the machine (none, push, PR or direct).
