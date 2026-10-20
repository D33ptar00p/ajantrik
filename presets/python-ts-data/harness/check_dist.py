"""Check the data payload the web app fetches: present, parseable, attributed, not bloated.

    check_dist.py <worktree> --base-mb <main checkout's data/dist size> [--out data.json]

The pipeline's own provenance gate runs only when the pipeline runs. This check runs every gate,
so a web change that fetches a file the pipeline never writes, or a pipeline change that ships a
source the registry can't attribute, fails before any reviewer looks at it.

Fails when:
  - a file in REQUIRED, or any `data/...` literal under web/src, is missing from data/dist/
  - a .json/.geojson file does not parse
  - manifest.json cites a source id that registry.yaml lacks, or a source without licence,
    attribution or url
  - fuels.json is not a list of strings
  - data/dist grew by more than MAX_GROWTH over the main checkout's payload
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

# Fetched by web/src through dataUrl() with a computed base, so the literal scan can't see them.
REQUIRED = [
    "manifest.json",
    "fuels.json",
    "assets.points.json",
    "assets.detail.idx.json",
    "assets.detail.bin",
    "gb/regions.geojson",
    "gb/plants.geojson",
    "gb/lines.geojson",
    "gb/adjacency.json",
    "gb/connections.json",
    "gb/reach.json",
]
MAX_GROWTH = 0.20
LITERAL = re.compile(r"""["'`]/?data/([\w./-]+\.(?:json|geojson|bin))["'`]""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("worktree", type=Path)
    ap.add_argument("--base-mb", type=float, required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    root = args.worktree.resolve()
    dist = root / "data" / "dist"
    problems: list[str] = []

    wanted = set(REQUIRED)
    for ts in (root / "web" / "src").rglob("*.ts"):
        wanted.update(LITERAL.findall(ts.read_text()))

    for rel in sorted(wanted):
        f = dist / rel
        if not f.is_file():
            problems.append(f"missing: data/dist/{rel} (fetched by the web app)")
            continue
        if f.suffix in (".json", ".geojson"):
            try:
                json.loads(f.read_text())
            except ValueError as e:
                problems.append(f"unparseable: data/dist/{rel}: {e}")

    try:
        fuels = json.loads((dist / "fuels.json").read_text())
        if not (isinstance(fuels, list) and all(isinstance(x, str) for x in fuels)):
            problems.append("fuels.json is not a list of fuel ids")
    except (OSError, ValueError):
        pass  # reported above

    registry = yaml.safe_load((root / "pipeline" / "sources" / "registry.yaml").read_text())
    known = registry.get("sources", {})
    try:
        manifest = json.loads((dist / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {"sources": []}
    for src in manifest.get("sources", []):
        sid = src.get("id")
        entry = known.get(sid)
        if entry is None:
            problems.append(f"manifest cites source '{sid}', which registry.yaml does not define")
            continue
        for key in ("licence", "attribution", "url"):
            if not entry.get(key):
                problems.append(f"registry source '{sid}' has no {key}")

    size_mb = round(sum(p.stat().st_size for p in dist.rglob("*") if p.is_file()) / 1e6, 1)
    limit = round(args.base_mb * (1 + MAX_GROWTH), 1)
    if args.base_mb and size_mb > limit:
        problems.append(
            f"data/dist is {size_mb} MB, over {limit} MB ({MAX_GROWTH:.0%} above the base "
            f"{args.base_mb} MB); every visitor downloads this"
        )

    print(f"data: {len(wanted)} fetched files checked, {len(manifest.get('sources', []))} sources, "
          f"{size_mb} MB (base {args.base_mb} MB)")
    for p in problems:
        print(f"  FAIL {p}")
    if args.out:
        args.out.write_text(json.dumps({"data_mb": size_mb, "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
