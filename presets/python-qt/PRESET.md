# Python + Qt/QML desktop app (mocap-desktop-gui)

Reproduces the Genesis Post factory for `mocap-desktop-gui` (Python + PySide6 +
QML, shared virtualenv, pre-commit, pytest with offscreen Qt).

- Builders: `py` (Python, Qt properties/signals/slots, tests) and `qml` (views,
  icons, qrc and generated `rc_*.py`).
- Routed reviewers: `calib` (calibration and project-data formats and their
  migrations), `xplat` (macOS, Ubuntu and Windows differences), `ui` and `ux`
  (QML and the Python properties it binds to).
- Gate: pre-commit over the branch's changes, pytest with coverage
  (`QT_QPA_PLATFORM=offscreen`). Set `FACTORY_VENV` if the venv is not at
  `~/.virtualenvs/mocap-desktop-gui`.
- Tickets: Jira `GEN`, read through the Atlassian MCP; the base is the epic's
  branch (`*<EPIC>_*` on origin) or `unstable`.
- Delivery: none (the user pushes and opens the PR from the printed commands).

Adapt: the repo name, venv path, path globs and the four repo skills
(`mc-signal-conventions`, `mc-migration-checklist`, `mc-test-patterns`,
`qml-ui-conventions`).

## Gate knobs

`FACTORY_GATE_<KEY>=gate|report|off` overrides a step's mode for one run, e.g.
`FACTORY_GATE_LINT=report`. Gate failures go to the `py` builder.
