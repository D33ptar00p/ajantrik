# Contributing to Ajantrik

Thanks for helping. Bug reports, docs fixes, new guardrails, agents, skills and runtime backends are
all welcome.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Security problems go through [SECURITY.md](SECURITY.md), never a public issue.
- By contributing you agree your work is released under the [MIT License](LICENSE), and you agree
  to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Set up

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/D33ptar00p/ajantrik.git
cd ajantrik
uv sync --extra api
uv run pre-commit install
uv run factory render      # writes .claude/settings.json (not tracked: it holds local paths)
```

The pre-commit hooks (`.pre-commit-config.yaml`) run whitespace and YAML checks, ruff, gitleaks,
mypy and pytest. Every commit must pass them.

## Make a change

1. Branch from `main`: `feature/<short-description>`, `bugfix/<short-description>` or `chore/<short-description>`.
2. Write tests alongside the code. New code needs unit tests; changes to the loop, runtimes or
   delivery need an end-to-end test too (see `tests/`).
3. If you change this repo's own `.factory/config/`, copy the change into `presets/default/` as
   well: the preset tests check that they match.
4. Run the checks:

   ```bash
   uv run pytest
   uv run ruff check ajantrik tests
   uv run mypy
   ```

5. Commit with a [Conventional Commits](https://www.conventionalcommits.org/) subject, as in the
   history: `feat: …`, `fix: …`, `test: …`, `docs: …`, `chore: …`.
6. Open a pull request against `main` describing what changed and why. CI runs the same checks on
   Python 3.11, 3.12 and 3.13.

## Code style

- Follow the patterns already in the file you are editing.
- Python: type hints, explicit and readable code, ruff's rules (`pyproject.toml`).
- `ajantrik/core/` is copied into every configured repo and must stay **stdlib only**.
- The studio (`ajantrik/studio/web/`) has no build step: plain JavaScript and CSS.

More detail on the internals: [docs/architecture.md](docs/architecture.md) and
[docs/development.md](docs/development.md).
