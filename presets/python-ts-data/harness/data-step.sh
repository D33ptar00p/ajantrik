#!/usr/bin/env bash
# Gate step "data" for the python-ts-data preset: check_dist.py over the worktree's payload, plus the
# size metrics the deploy reviewer is routed on.
#
#   data-step.sh          (run by the gate in the worktree; uses REPO, WORKTREE, OUT, FACTORY_DIR)
#
# Writes $OUT/data.json (check_dist.py) and $OUT/data-metrics.json:
#   {"data_mb": 64.1, "base_data_mb": 64.0, "data_mb_delta": 0.1}
set -uo pipefail

PY="$REPO/.venv/bin/python"
size_mb() {
    "$PY" -c "import sys,pathlib; print(round(sum(p.stat().st_size for p in pathlib.Path(sys.argv[1]).rglob('*') if p.is_file())/1e6,1))" "$1"
}
BASE_MB="$(size_mb "$REPO/data/dist")"
echo "data: base payload ${BASE_MB} MB"
"$PY" "$FACTORY_DIR/harness/check_dist.py" "$WORKTREE" --base-mb "$BASE_MB" --out "$OUT/data.json"
RC=$?
"$PY" - "$OUT/data.json" "$BASE_MB" "$OUT/data-metrics.json" <<'PY'
import json, sys
try:
    mb = json.load(open(sys.argv[1])).get("data_mb")
except Exception:
    mb = None
base = float(sys.argv[2])
out = {"data_mb": mb, "base_data_mb": base, "data_mb_delta": round(mb - base, 1) if mb is not None else None}
json.dump(out, open(sys.argv[3], "w"))
print(f"data: payload {mb} MB (delta {out['data_mb_delta']})")
PY
exit $RC
