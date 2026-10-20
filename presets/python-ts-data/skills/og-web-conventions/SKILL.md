---
name: og-web-conventions
description: How OmniGrid's web app is built - vanilla TypeScript on Vite with MapLibre GL, module layout, data loading through dataUrl(), provenance badges, panels, styling tokens and the GitHub Pages base path. Preloaded by factory agents that read or change web/.
---

# OmniGrid web conventions

## Stack

Vite 6, strict TypeScript (`noUnusedLocals`, `noUncheckedIndexedAccess`),
`maplibre-gl` 5, `h3-js`, `pmtiles`. **No UI framework**: the HTML shell is
`web/index.html`, and TypeScript modules wire the DOM by id. Basemap:
`https://tiles.openfreemap.org/styles/dark` (no key). No JS test runner.

```bash
cd web && npm run dev        # 127.0.0.1:5173
cd web && npm run build      # tsc --noEmit && vite build
```

## Layout

| Path | What it is |
|---|---|
| `web/src/main.ts` | Boot: map, World layer (`assets` source, `assets-circles` layer), legend, filters (fuel, operator, status), view toggle (`setView("gb" \| "global")`), tooltips, banner. Exposes `window.__map` in dev only. |
| `web/src/gb/view.ts` | GB sources and layers (regions, plants, lines). |
| `web/src/gb/trace.ts` | `GridTracer`: BFS over `adjacency.json` from a plant, hop slider. |
| `web/src/panels/detail.ts` | World plant panel. `panels/gb.ts`: GB plant panel. `panels/about.ts`: Data sources panel. |
| `web/src/lib/paths.ts` | `dataUrl(path)`: resolves against `import.meta.env.BASE_URL`. |
| `web/src/lib/provenance.ts` | `Cited`, `Kind`, `KIND_TITLE`, `Manifest`, `Sources` (`short(id)`, `get(id)`). |
| `web/src/lib/palette.ts` | `FUEL_COLOR`, `FUEL_LABEL`, `FUEL_ORDER`, `fuelColorExpression()`. |
| `web/src/lib/assets.ts` | `AssetStore`: points at boot, detail rows on demand from the binary file. |
| `web/src/lib/format.ts` | `power()` and number formatting. |
| `web/src/lib/panels.ts` | `togglePanel`, `closeBottomPanels`: legend and sources share one slot. |
| `web/src/styles.css` | Tokens on `:root`: `--bg`, `--panel`, `--panel-solid`, `--line`, `--ink`, `--ink-dim`, `--ink-faint`, `--accent`, `--font`, `--mono`. Provenance badges: `.kind.measured`, `.kind.derived`, `.kind.linked`. |

Key DOM ids: `map`, `brand`, `view-note`, `views` (buttons `data-view="gb"`/`"global"`),
`layers` (`layer-regions`, `layer-lines`), `statusfilter`/`status-select`,
`opfilter`/`operator-select`, `banner`, `controls`, `legend-toggle`,
`about-toggle`, `legend`/`legend-items`/`legend-gb`, `panel`, `tooltip`, `about`.

## Rules

- **Every data fetch goes through `dataUrl()`.** The site is served at `/` in
  dev and `/omnigrid/` on GitHub Pages; an absolute `/data/...` 404s in
  production while the map still draws. The gate builds with the Pages base.
- **Data shapes come from the pipeline.** Fuel list and order from
  `fuels.json`, sources and attribution from `manifest.json`. Don't
  hard-code what the pipeline ships, and don't compute in the browser what the
  pipeline should ship.
- **Text from data is hostile.** Plant names, operators, Wikidata labels and
  photo credits come from open-edit sources. Use `textContent`, or the
  `escapeHtml` helper when building HTML strings; URLs from data must be
  `https:`; external links get `rel="noopener noreferrer"`.
- **Provenance is visible.** Every value in a panel carries its badge
  (`<span class="kind measured|derived|linked" title="...">`) and source
  short name, as `panels/gb.ts` does.
- **MapLibre**: add sources and layers once at boot and toggle `visibility`;
  express styling as layer expressions; keep per-frame handlers cheap; large
  files load when their view opens.
- **No new dependencies or origins** without a spec that says so.

## Style

Small focused modules in `lib/`, `panels/`, `gb/`. JSDoc blocks explain the
reason behind a decision ("Clicking a fuel isolates it... Selecting is what
people reach for"). Double quotes, semicolons, two-space indent. Sentence case
in UI copy; units always shown (MW, GW, kV).
