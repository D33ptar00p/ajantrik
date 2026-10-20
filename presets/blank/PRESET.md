# Blank factory

A repo-neutral starting point: the full loop (spec, design, approval, tests, one
builder, gate, code and security review, integrate, deliver) with the generic
agents and skills from the library.

## Start

1. `factory init --preset blank` in the repo, then open the studio
   (`factory studio`).
2. **Repo**: set the name and the default base branch.
3. **Gate**: add one step per check the repo already runs in CI (tests with
   `junit`, lint, typecheck, build). Without steps the gate only scans for
   secrets and checks the tree is clean.
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
