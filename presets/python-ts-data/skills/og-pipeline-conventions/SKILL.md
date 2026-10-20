---
name: og-pipeline-conventions
description: How OmniGrid's Python data pipeline is built - source readers, the registry, Cited values, the schema's fail-loud taxonomy, the normalise flow and the data/dist outputs. Preloaded by factory agents that read or change pipeline/.
---

# OmniGrid pipeline conventions

## Layout and flow

| Path | What it is |
|---|---|
| `pipeline/sources/registry.yaml` | Every source: name, short, publisher, version, url, download_url, licence, licence_url, attribution, access (`direct`/`form`/`derived`), role, raw_glob, provides, notes. The single source of truth for attribution. |
| `pipeline/sources/registry.py` | Loads and enforces it (`REQUIRED_FIELDS`, `VALID_ACCESS`, `VALID_ROLES`). |
| `pipeline/sources/fetch.py` | `fetch(source)` downloads into `data/raw/<source_id>/` and checksums; `find_vendored(source)` finds hand-downloaded files and raises `MissingVendoredInput` with where to get them. |
| `pipeline/sources/<id>.py` | One reader per source (`gem.py`, `wri.py`, `gb.py`, `wikidata.py`, `ember.py`). Reads raw files, returns typed records with `Cited` values. |
| `pipeline/schema.py` | `Asset`, `Fuel`, `Status`, `AssetKind`. `parse_fuel()` maps source strings or raises `UnknownFuel`; storage is excluded upstream. |
| `pipeline/provenance.py` | `Kind`, `Cited[T]`, `measured()`, `derived()`, `collect_source_ids()`. |
| `pipeline/gb.py`, `pipeline/grid.py` | Great Britain build: OSM plants, Elexon units, DNO regions, metered output, the line graph and per-plant reach. |
| `pipeline/pack.py` | Packs global assets column-wise into `assets.points.json` + `assets.detail.bin` + index. |
| `pipeline/manifest.py` | Writes `manifest.json` and regenerates `DATA_SOURCES.md` from the registry. |
| `pipeline/normalize.py` | `run()`: load GEM (WRI fallback) → provenance gate (`BuildFailed` on an unregistered or unlicensed source) → GB build → pack → manifest, `fuels.json`, `DATA_SOURCES.md`. |

Run it with `.venv/bin/python -m pipeline.normalize` (in a factory worktree:
`PYTHONPATH=<worktree> <repo>/.venv/bin/python -m pipeline.normalize`). It
reads `data/raw/`, writes `data/dist/`. `data/` is gitignored; never commit it.

`data/dist/` outputs the web app reads: `manifest.json`, `fuels.json`,
`assets.points.json`, `assets.detail.idx.json`, `assets.detail.bin`, and
`gb/{plants,lines,regions}.geojson`, `gb/{units,connections,adjacency,reach}.json`.

## Rules

- **Every value is cited.** Use `measured(value, source_id)` or
  `derived(value, method, source_id, *via)`. Serialised as
  `{"v", "k", "s", "m"?, "via"?}` (`Cited.to_json`). `modelled()` exists from
  the removed supply-shed model; don't use it: OmniGrid ships nothing modelled.
- **Fail loud.** Unknown categorical values raise (`UnknownFuel`,
  `UnknownFuelType`); unreadable numbers return `None`
  (`parse_power_mw`), never a guess or a zero. Dropped rows are counted in the
  build report so they appear in the manifest, not lost.
- **Judgements are labelled.** Name-based matches (OSM plant ↔ Elexon unit,
  operator ↔ company) are record links, shown as `linked`, with the rule stated.
- **Registry first.** A new source gets its registry entry before any code
  cites it; the build fails otherwise.
- **Single sources of truth.** Fuel order is data (`fuels.json`), not a
  frontend constant. Attribution comes from the registry.
- **No new dependencies.** `pyproject.toml` lists what exists: pyyaml, polars,
  numpy, scipy, h3, requests, openpyxl, xlrd (+ `[geo]`: rasterio, pyogrio,
  shapely, duckdb). GeoPandas is deliberately not used.

## Style

- `from __future__ import annotations`, full type hints, `pathlib`,
  dataclasses, `str` Enums. Stdlib `csv`/`json`/`zipfile` where they suffice.
- Every module opens with a docstring that says why it exists and what it
  does not do ("Nothing in this module is modelled..."). Comments explain
  reasons, not mechanics.
- Short snake_case module names. Ruff, line length 100, py312. The repo has
  existing ruff findings; the gate only fails when a change adds new ones.
- `requests` calls have a timeout; YAML through `yaml.safe_load`.

## Known stale bits (don't copy them)

- `DATA_SOURCES.md` and `fetch.py` mention `make attribution` / `make fetch`;
  there is no Makefile. Run the modules directly.
- `registry.yaml` still lists sources from the removed model (omnigrid_model,
  kontur, ghsl, gridfinder, ...), and `ember.py`/`wri.py` docstrings mention
  modelling. Don't build on them without a spec that says so.
