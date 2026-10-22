#!/usr/bin/env python3
"""PII scan for a factory job: personal data added on the branch (stdlib only).

    pii_scan.py --out <pii.json> --git <worktree> <merge-base> [--exclude GLOB]...

Looks at every line the branch adds (merge-base..HEAD) for email addresses, phone numbers, US
social security numbers, payment card numbers (Luhn-checked) and IBANs (checksum-checked). Findings
name the file, line, rule and a fingerprint; the value itself is never printed or written. Exit 0
clean, 1 findings, 2 usage error.

Placeholder data (example.com addresses, 555 numbers, well-known test card numbers) is ignored. A
reviewed false positive: add its fingerprint to allow.txt next to this script, with a comment.
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

# Lock files, minified bundles and the factory's own files are noise, not people's data.
DEFAULT_EXCLUDE = ["*.lock", "*lock.json", "*.min.js", "*.map", "*.svg", "*.snap", ".factory/*"]

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
PLACEHOLDER_DOMAINS = re.compile(r"@(?:[\w-]+\.)*(example\.(?:com|org|net)|test|invalid|localhost|users\.noreply\.github\.com)$",
                                 re.I)
PHONE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]\d{3,4}[ .-]\d{3,4}(?!\w)")
SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
# Published test numbers (Stripe, PayPal, Visa docs): fine in tests and fixtures.
TEST_CARDS = {"4242424242424242", "4111111111111111", "5555555555554444", "378282246310005", "4000056655665556",
              "5105105105105100", "6011111111111117", "4012888888881881"}


def luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    s = value.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def findings_in(line: str) -> list[tuple[str, str]]:
    """(rule, value) pairs in one added line."""
    out: list[tuple[str, str]] = []
    for m in EMAIL.finditer(line):
        if not PLACEHOLDER_DOMAINS.search(m.group(0)):
            out.append(("email", m.group(0).lower()))
    for m in SSN.finditer(line):
        out.append(("us-ssn", m.group(0)))
    for m in CARD.finditer(line):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn(digits) and digits not in TEST_CARDS and len(set(digits)) > 2:
            out.append(("payment-card", digits))
    for m in IBAN.finditer(line):
        if iban_ok(m.group(0)):
            out.append(("iban", m.group(0).replace(" ", "")))
    # A long run of digit groups is a card-shaped number (valid or not), never a phone number.
    numbers = [m.span() for m in CARD.finditer(line)]
    for m in PHONE.finditer(line):
        digits = re.sub(r"\D", "", m.group(0))
        inside = any(a <= m.start() and m.end() <= b for a, b in numbers)
        if 9 <= len(digits) <= 15 and not inside and not re.search(r"555\d{4}$", digits):
            out.append(("phone", digits))
    return out


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


def scan(worktree: str, merge_base: str, exclude: list[str], allow: set[str]) -> list[dict]:
    findings = []
    for path, line, text in added_lines(worktree, merge_base):
        if any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(Path(path).name, g) for g in exclude):
            continue
        for rule, value in findings_in(text):
            fp = fingerprint(rule, value)
            if fp not in allow:
                findings.append({"file": path, "line": line, "rule": rule, "fingerprint": fp})
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--git", nargs=2, metavar=("WORKTREE", "MERGE_BASE"), required=True)
    ap.add_argument("--exclude", action="append", default=[])
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    try:
        findings = scan(a.git[0], a.git[1], DEFAULT_EXCLUDE + a.exclude, load_allow())
    except subprocess.CalledProcessError as e:
        print(f"pii-scan: git diff failed: {e.stderr.strip()[-200:]}", file=sys.stderr)
        return 2
    Path(a.out).write_text(json.dumps({"tool": "pii_scan", "findings": findings}, indent=2) + "\n")
    for f in findings:
        print(f"pii-scan: {f['file']}:{f['line']}: {f['rule']} (fingerprint {f['fingerprint']})")
    print(f"pii-scan: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
