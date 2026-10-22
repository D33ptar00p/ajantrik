#!/usr/bin/env python3
"""Topic, word and regex guardrails: forbidden content added on the branch (stdlib only).

    content_scan.py --out <out.json> --git <worktree> <merge-base>
                    [--topics "name: word, phrase; name2: ..."] [--regex PATTERN]... [--path GLOB]...

--topics  named lists of words or phrases, matched case-insensitively on word boundaries
          ("competitors: vicon, optitrack; codenames: project falcon").
--regex   Python regexes, matched case-sensitively; give one per flag, or one per line in a single
          value. Use (?i) for case-insensitive.
--path    only scan added lines in files matching these globs (default: every file).

Findings name the file, line, rule (topic:<name> or regex:<n>) and a fingerprint; the matched text is
never printed or written, so a banned word does not spread into logs. Exit 0 clean, 1 findings,
2 usage error. A reviewed false positive: add its fingerprint to allow.txt next to this script.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The factory's own files hold the guardrail settings themselves (the topics and patterns).
DEFAULT_EXCLUDE = [".factory/*"]


def parse_topics(spec: str) -> dict[str, re.Pattern[str]]:
    """'name: a, b; other: c' -> {name: /\\b(a|b)\\b/i, other: /\\bc\\b/i}."""
    out: dict[str, re.Pattern[str]] = {}
    for part in [p for p in re.split(r"[;\n]", spec or "") if p.strip()]:
        name, _, words = part.partition(":") if ":" in part else ("words", ":", part)
        terms = [w.strip() for w in words.split(",") if w.strip()]
        if terms:
            alt = "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in sorted(terms, key=len, reverse=True))
            out[name.strip() or "words"] = re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.I)
    return out


def parse_regexes(values: list[str]) -> list[re.Pattern[str]]:
    pats = [ln.strip() for v in values for ln in v.splitlines() if ln.strip()]
    return [re.compile(p) for p in pats]


def fingerprint(rule: str, value: str) -> str:
    return hashlib.sha256(f"{rule}:{value}".encode()).hexdigest()[:16]


def load_allow(path: Path = HERE / "allow.txt") -> set[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    return {ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()}


def added_lines(worktree: str, merge_base: str) -> list[tuple[str, int, str]]:
    diff = subprocess.run(["git", "-C", worktree, "diff", "-U0", "--no-color", "--no-ext-diff", f"{merge_base}...HEAD"],
                          capture_output=True, text=True, errors="replace", check=True).stdout
    out, path, line = [], "", 0
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            path = ln[6:] if ln.startswith("+++ b/") else ""
        elif ln.startswith("@@"):
            m = re.search(r"\+(\d+)", ln)
            line = int(m.group(1)) if m else 0
        elif ln.startswith("+") and path:
            out.append((path, line, ln[1:]))
            line += 1
    return out


def scan(lines: list[tuple[str, int, str]], topics: dict[str, re.Pattern[str]], regexes: list[re.Pattern[str]],
         paths: list[str], allow: set[str]) -> list[dict]:
    rules = [(f"topic:{n}", p) for n, p in topics.items()] + [(f"regex:{i + 1}", p) for i, p in enumerate(regexes)]
    findings = []
    for path, line, text in lines:
        if paths and not any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(Path(path).name, g) for g in paths):
            continue
        if any(fnmatch.fnmatch(path, g) for g in DEFAULT_EXCLUDE):
            continue
        for rule, pat in rules:
            for m in pat.finditer(text):
                fp = fingerprint(rule, m.group(0).lower())
                if fp not in allow:
                    findings.append({"file": path, "line": line, "rule": rule, "fingerprint": fp})
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--git", nargs=2, metavar=("WORKTREE", "MERGE_BASE"), required=True)
    ap.add_argument("--topics", default="")
    ap.add_argument("--regex", action="append", default=[])
    ap.add_argument("--path", action="append", default=[])
    try:
        a = ap.parse_args(argv)
        topics, regexes = parse_topics(a.topics), parse_regexes(a.regex)
    except SystemExit:
        return 2
    except re.error as e:
        print(f"content-scan: bad regex: {e}", file=sys.stderr)
        return 2
    if not topics and not regexes:
        print("content-scan: nothing to look for (set the topics or patterns)", file=sys.stderr)
        return 2
    try:
        findings = scan(added_lines(a.git[0], a.git[1]), topics, regexes, a.path, load_allow())
    except subprocess.CalledProcessError as e:
        print(f"content-scan: git diff failed: {e.stderr.strip()[-200:]}", file=sys.stderr)
        return 2
    Path(a.out).write_text(json.dumps({"tool": "content_scan", "findings": findings}, indent=2) + "\n")
    for f in findings:
        print(f"content-scan: {f['file']}:{f['line']}: {f['rule']} (fingerprint {f['fingerprint']})")
    print(f"content-scan: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
