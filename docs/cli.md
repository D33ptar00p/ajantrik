# CLI reference

Every `factory` command. Run `factory --help` or `python3 .factory/factory.py --help` for the full list.

```bash
factory init [--preset NAME] [--name REPO] [--force]   # create .factory/config/ from a preset, then render
factory presets                                        # list presets
factory validate                                       # check the config (errors and warnings)
factory render [--check]                               # write .claude/ and .factory/ from the config
factory studio [--port N] [--no-browser] [--all]       # the web studio
factory projects                                       # list projects
factory projects add <path> [--runtime R] [--preset P] [--name N]   # register a repo, setting up its factory
factory projects remove <id>                           # forget a project (its files stay)
factory doctor [--only git|tickets|runtime|sandbox]    # check connections, the runtime and the sandbox
factory run "<request>" [--ticket K] [--base B] [--wait]   # headless: create a job and drive it
factory go <job> [--wait]                              # headless: approve the waiting stage and continue
factory resume <job> [--wait]                          # headless: continue a job
factory next [--wait] [--create-only] [--items 0,2]    # claim backlog items and run them (see Backlog)
factory new|step|advance|refresh|gate|approve|rework|deliver|abandon|status|sync|route|ticket
```

The last line is the job commands: they are the same as `python3 .factory/factory.py <cmd>`
(`python3 .factory/factory.py --help` lists them). `--json` anywhere on the line prints
machine-readable output, which the studio relies on. `--root <path>` points at a repo other than
the current one. `--wait` waits at approvals instead of stopping.

---

[← Docs index](README.md) · [Project README](../README.md)
