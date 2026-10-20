# Python data pipeline + TypeScript map app (omnigrid)

Reproduces the OmniGrid factory: a Python pipeline that builds a data payload
(`data/dist`) and a vanilla TypeScript + Vite + MapLibre web app that renders it,
deployed to GitHub Pages.

- Builders: `pipeline` (sources, schema, normalise, tests) and `web` (the app).
- Routed reviewers: `provenance` (licences, attribution, measured vs modelled
  data), `deploy` (build, base path, new origins, payload growth over 1 MB via
  the `data_mb_delta` gate metric), `ui` and `ux`.
- Gate: ruff ratchet, pytest, the pipeline (only when `pipeline/` changed), a
  payload check (`harness/data-step.sh` + `check_dist.py`), `tsc`, `vite build`
  with the Pages base, and a smoke test of both views on `vite preview`.
- Worktree setup: links to the shared `web/node_modules`, `data/raw` and
  `data/interim`, and a private copy of `data/dist`.
- No ticket system: jobs start from a free-text request or the backlog.

Adapt: `harness/check_dist.py` (its REQUIRED list and registry checks are
specific to this payload), `harness/smoke.json`, and the `og-*` skills.

## Gate knobs

Each gate step's mode can be overridden per run with
`FACTORY_GATE_<KEY>=gate|report|off`, replacing the old knobs:
`FACTORY_GATE_E2E=report|off` for the smoke test (was `FACTORY_E2E`), and
`FACTORY_GATE_PIPELINE=off` to skip the pipeline rebuild (was
`FACTORY_PIPELINE=never`). The pipeline otherwise runs only when `pipeline/`
changed; there is no "always" mode any more. Gate failures are routed by step:
lint, tests, pipeline and data go to `pipeline`; typecheck, build and e2e go to
`web`.
