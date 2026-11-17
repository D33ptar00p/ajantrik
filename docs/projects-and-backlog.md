# Projects and backlog

Manage several repositories from one studio, and queue work for `factory next`.

## Projects

A project is one git repository with its own factory. The registry only remembers where they are:
`$AJANTRIK_HOME/projects.json` (default `~/.ajantrik`).

```bash
factory projects                          # list the registered repos
factory projects add ~/code/my-repo       # register a repo; if it has no factory, sets one up from --preset and --runtime
factory projects remove <id>              # forget a project: the repo and its .factory/ stay as they are
```

`factory studio` run outside a configured repo, or with `--all`, opens the Projects page.

## Backlog

`.factory/backlog.md` is the queue of work for `factory next`. Items go under `## Queue`; add them
by hand or with `factory sync`:

```
- [ ] PROJ-781 · base: feature/PROJ-750_Projects · Add project rename    (ticket)
- [ ] base: main · Escape plant names in tooltips                          (free text)
- [!] PROJ-795 · epic PROJ-802 has no branch on origin · User profile export
- [x] PROJ-781 · base: ... · Add project rename → 004-PROJ-781-project-rename
```

- `- [ ]` is queued: a ticket, or free text as `base: <branch> · <request>`.
- `- [x]` is done, with `→ <job id>`.
- `- [!]` is blocked until a human resolves it.
- `factory sync` fills the file from the ticket system. `factory sync --push` first creates issues
  for queued lines that have no ticket. It writes to the ticket system, so it is a human's command.
- `factory next` claims the first `- [ ]` item and runs it headless. `--create-only` only creates
  the job. `--items 0,2` picks items by their 0-based position instead.
- The studio's Backlog tab shows the same file. When the runtime is headless, ticked queued items
  can be started as one job there (they must share one ticket and one base branch).

---

[← Docs index](README.md) · [Project README](../README.md)
