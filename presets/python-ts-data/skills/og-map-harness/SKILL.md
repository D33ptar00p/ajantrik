---
name: og-map-harness
description: How factory agents render OmniGrid's map for screenshots - with-app.sh and shoot.mjs, the shots.json step vocabulary (view toggle, jumpTo, clickAt, mapIdle), naming for before/mockup/after sets, and scratch checkouts of the base. Preloaded by the architect, builder-web, reviewer-ui and reviewer-ux.
---

# Map screenshot harness

`.factory/harness/` drives the system Chrome through `playwright-core`
(installed once with `npm ci --prefix .factory/harness`). It is read-only: any
request that isn't GET/HEAD/OPTIONS is aborted.

## Serve and shoot

```bash
cd <repo> && .factory/harness/with-app.sh <checkout> -- node .factory/harness/shoot.mjs <shots.json>
```

- `with-app.sh` takes `.factory/.port.lock` (shared with the gate and every
  other job: expect to wait), starts `vite` in `<checkout>/web` on
  `127.0.0.1:5199`, runs the command with `APP_URL` set, then stops the server.
  The checkout needs `web/node_modules` and `data/dist` (links are fine).
- Dev mode (the default) exposes `window.__map`, which `mapIdle`, `jumpTo`
  and `clickAt` use. `--preview <dist>` serves a production build instead (the
  gate's smoke does this); it has no `__map`.
- `shoot.mjs --strict` exits 1 on page errors, console errors or failed
  requests to the app. Without it, failures are only reported.
- Each run writes `<outDir>/<shots>.report.json`: per shot, `console`,
  `pageErrors`, `failedRequests` (app), `externalFailures` (tiles, photos),
  `blocked`. Read it.

## shots.json

```json
{
  "outDir": "<job folder>/round-2/shots",
  "shots": [
    {
      "name": "gb-drax-selected-after",
      "path": "/",
      "viewport": "desktop",
      "steps": [
        { "waitFor": "#views:not([hidden])" },
        { "mapIdle": true },
        { "jumpTo": { "center": [-0.996, 53.737], "zoom": 12 } },
        { "clickAt": [-0.996, 53.737] },
        { "waitFor": "#panel:not([hidden])" }
      ]
    },
    {
      "name": "world-nuclear-filter-after-mobile",
      "path": "/",
      "viewport": "mobile",
      "steps": [
        { "waitFor": "#views:not([hidden])" },
        { "click": "#views button[data-view=global]" },
        { "click": "#legend-toggle" },
        { "click": "#legend-items >> text=Nuclear" },
        { "mapIdle": true }
      ]
    }
  ]
}
```

`outDir` must be absolute. Viewports: `desktop` 1440×900, `mobile` 390×844, or
`{"width", "height"}`. Steps: `waitFor`, `waitForText`, `click`, `hover`,
`fill`, `select` (`["#operator-select", "<label>"]`), `press`, `wait` (ms),
`scrollTo`, `mapIdle`, `jumpTo` (MapLibre `jumpTo` options, then waits for
idle), `clickAt` (`[lng, lat]`, clicks that map position), `shot` (extra PNG
mid-flow). The final PNG is `<name>.png`; on failure `<name>-error.png`.

The app boots into the Great Britain view; `#views:not([hidden])` means it has
finished booting. Always `mapIdle` before a shot: tiles and GeoJSON load
asynchronously and an early shot shows a half-drawn map.

Useful places: Drax `[-0.996, 53.737]` (GB, large, photographed, traced);
London `[-0.12, 51.5]` z9 (dense); Hornsea offshore `[1.79, 53.88]` z8;
World view dense area: Ruhr `[7.0, 51.45]` z8, eastern China `[118, 32]` z5.

## Naming

`<view>-<state>-<before|mockup|after>[-mobile].png`, e.g.
`gb-drax-selected-before.png`, `gb-drax-selected-after.png`. Every member of a
set uses the same stem so the dashboard shows them side by side. UX steps:
`ux-<criterion>-<step>-<before|after>.png`.

## Before shots: a scratch checkout of the base

```bash
cd <worktree> && git worktree add --detach <scratch>/base <base>
ln -s <repo>/web/node_modules <scratch>/base/web/node_modules
mkdir -p <scratch>/base/data && ln -s <repo>/data/dist <scratch>/base/data/dist
cd <repo> && .factory/harness/with-app.sh <scratch>/base -- node .factory/harness/shoot.mjs <scratch>/before.json
cd <worktree> && git worktree remove --force <scratch>/base
```

The base renders against the main checkout's `data/dist` (the data before the
change); the branch renders against the worktree's own `data/dist`. Drive both
with identical steps. The architect's mockup checkout is the same, linked to
the worktree's `data/dist` so new fields can be shown.

## Look at every PNG

Open each image you produce (Read the PNG) before you cite it. A shot of a
blank map, a half-loaded basemap or an error overlay is not evidence.
