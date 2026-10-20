"""Fail only when a branch adds ruff findings to the files it touches.

    lint_ratchet.py <ruff> <merge-base> <file.py>...

The repo carries existing ruff findings, so "the changed file must be clean" would make every
touched file a cleanup job. Instead each changed file is linted at HEAD and at the merge-base (via
stdin, so nothing is checked out), and the check fails when any rule's count in a file goes up.
A new file is compared against zero. Uses the repo's own ruff configuration from pyproject.toml.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


def counts(ruff: str, path: str, source: str) -> Counter[str]:
    out = subprocess.run(
        [ruff, "check", "--output-format", "json", "--stdin-filename", path, "-"],
        input=source,
        capture_output=True,
        text=True,
    )
    if out.returncode not in (0, 1):
        raise SystemExit(f"ruff failed on {path}:\n{out.stderr}")
    return Counter(d["code"] or "syntax" for d in json.loads(out.stdout or "[]"))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    ruff, base, *files = argv
    worse = 0
    for path in files:
        if not Path(path).exists():
            continue
        head = counts(ruff, path, Path(path).read_text())
        old = subprocess.run(["git", "show", f"{base}:{path}"], capture_output=True, text=True)
        before = counts(ruff, path, old.stdout) if old.returncode == 0 else Counter()
        for code, n in sorted(head.items()):
            if n > before[code]:
                worse += 1
                print(f"{path}: {code} {before[code]} -> {n}")
    if worse:
        print(f"\nlint: {worse} rule count(s) went up. Run: .venv/bin/ruff check <file> to see them.")
        return 1
    print(f"lint: no new findings in {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
