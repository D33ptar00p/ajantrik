# Development

Working on Ajantrik itself. See also [CONTRIBUTING.md](../CONTRIBUTING.md).

```bash
uv sync --extra api
uv run pytest            # unit, end-to-end loop (scripted backend), backends (fake CLIs), the default preset
uv run ruff check ajantrik tests && uv run mypy
```

The preset tests materialise `presets/default` into a temp repo, render it, and check its agents,
skills and loop match this repo's own `.factory/config/` (copy changes there into the preset too).

---

[← Docs index](README.md) · [Project README](../README.md)
