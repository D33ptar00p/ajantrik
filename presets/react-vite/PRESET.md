# React + TypeScript + Vite web app (accounts-portal)

Reproduces the Accounts Portal factory (React, TypeScript, Vite, MUI, Vitest,
Cypress against a staging backend).

- Builders: `data` (contexts, hooks, utils, types, redux, auth, navigation,
  config) and `ui` (pages, components, layouts, theme, Cypress).
- Routed reviewers: `billing` (plans, Stripe, credits, seats), `platform`
  (build, deploy, env, analytics, CSP), `ui` and `ux` (staging screenshots).
- Gate: `tsc`, an eslint ratchet on changed files (`harness/lint-ratchet.mjs`),
  Vitest with coverage, `vite build`, and Cypress on `vite preview` under the
  port lock (an e2e failure outside the change goes to needs-human).
- Worktree setup: `npm ci` and a link to the main checkout's `.env` (which must
  point at staging; the preflight checks it without printing values).
- Harness: `npm ci --prefix .factory/harness` once (playwright-core with the
  system Chrome).
- Tickets: Jira `IST` through the Atlassian MCP; the base is the epic's branch
  (`*<EPIC>[_-]*`) or `develop`.

Adapt: the env keys in the preflight, the staging pattern, the builder areas,
reviewer routing and the four `ap-*` skills.

## Gate knobs

Each gate step's mode can be overridden per run with
`FACTORY_GATE_<KEY>=gate|report|off` (this replaces the old `FACTORY_E2E`):
`FACTORY_GATE_E2E=report` runs Cypress but never fails the gate, `off` skips
it. `FACTORY_APP_PORT` (default 5173, an allowed Auth0 callback) and
`FACTORY_TEST_TIMEOUT` (vitest per-test ms, default 20000) still apply.
