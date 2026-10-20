---
name: og-provenance-and-licensing
description: OmniGrid's rules for what the map may claim - measured vs derived vs linked values, the "nothing modelled" stance, the source licences and exact attribution strings, ODbL share-alike, and how provenance must show in the app. Preloaded by factory agents that decide or check what data ships.
---

# Provenance and licensing

OmniGrid's promise: every number on the map traces to a published source under
a licence that allows it, and the reader can tell at a glance what kind of
claim it is. **Nothing is modelled.** The supply-shed model was deliberately
removed (commit 48c6b36, "Drop all modelled data"); don't bring estimates back.

## Kinds of claim

| Kind | Meaning | In code | In the app |
|---|---|---|---|
| measured | Reported by a named source | `measured(v, source_id)` → `{"k": "measured"}` | `.kind.measured` badge |
| derived | Computed from cited sources by a named, simple formula (a sum, a unit conversion, a point-in-polygon) | `derived(v, method, source_id, *via)`; `method` is required | `.kind.derived` badge, method stated |
| linked | A record match we made (by name, distance or company), not an identifier the sources share | stated in the panel; the rule is shown | `.kind.linked` badge, with a title saying how it was matched |

- A missing value is `None` and shown as "not published", never 0 or a guess.
- A wrong kind is worse than a missing value. If in doubt, it is not measured.
- Anything estimated, imputed, interpolated, calibrated or forecast is
  modelled, and doesn't ship. A request that needs it is BLOCKED.

## Sources in use and their terms

| Source id(s) | Licence | Attribution (verbatim, from registry.yaml) |
|---|---|---|
| `gem_gipt` | CC BY 4.0 | "Global Integrated Power Tracker, Global Energy Monitor, August 2026 release." |
| `wri_gppd` (fallback) | CC BY 4.0 | "Global Power Plant Database v1.3.0, World Resources Institute." |
| `elexon_bmu`, `elexon_b1610` | Elexon BMRS open licence | "Contains BMRS data © Elexon Limited copyright and database right 2026." (exact string required) |
| `neso_dno_areas` | NESO Open Data Licence | as in registry.yaml |
| `osm_gb_power` | **ODbL 1.0, share-alike** | "© OpenStreetMap contributors, ODbL." |
| `wikidata` | CC0 (data); Commons photos per image | per photograph: author and licence beside the image |
| `natural_earth` | Public domain | "Made with Natural Earth." |

Attribution reaches the app through `manifest.json` (built from the registry)
and the Data sources panel; `DATA_SOURCES.md` is generated from the same
registry. Never hand-edit either output; change the registry and rebuild.

## Licence rules for a new source

- **Allowed**: public domain, CC0, CC BY, ODbL (share-alike noted), open
  government licences that allow redistribution with attribution.
- **Not allowed** (BLOCKED): non-commercial (NC), no-derivatives (ND), "for
  personal use", terms that forbid redistribution, or no stated licence.
- **Escalate always**: any new source, any licence change, and anything
  under `DATA_LICENCES.md` or `LICENSE`. A human signs licensing off.
- Sources behind a form (`access: form`, like GEM) are vendored by hand into
  `data/raw/<dir>/` and checksummed; the pipeline raises
  `MissingVendoredInput` with the download page.

## Share-alike

The GB build is substantially OSM-derived (plant locations, lines,
substations), so those parts of `data/dist/` are ODbL: keep the OSM credit
wherever they are shown, and don't mix them into a file presented under a
different licence.

## In the app

- Every value in a panel shows its badge and the source's `short` name, linked
  to the source.
- Caveats from the build report (`manifest.warnings`) reach the banner when
  they change what a reader would conclude.
- Data dates (`fetched`, `data_date`) are shown in the Data sources panel;
  a new source has them too.
