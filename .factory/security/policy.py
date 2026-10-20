#!/usr/bin/env python3
"""Shared rules for the factory's security hooks and secret scan (stdlib only).

    policy.py scan --out <secrets.json> [--git <worktree> <merge-base>] [--file <path>]...
    policy.py redact  < text > text
    policy.py check-env [--require KEY,...] [--match KEY,... --pattern REGEX]

`check-env` validates the repo's .env for the preflight without printing a value: it names
missing keys and keys whose value doesn't match the pattern (e.g. staging hosts only).

`scan` exits 1 when it finds anything. Findings name the file, line, rule and a
fingerprint; a secret's value is never printed or written. To accept a false
positive, add its fingerprint to allow.txt next to this file, with a comment.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ---- secret patterns -----------------------------------------------------------------------------
# Each rule's last capture group (or the whole match) is the secret; that is what gets fingerprinted.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key-id", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b")),
    ("aws-secret-access-key", re.compile(
        r"(?i)aws.{0,20}?(?:secret|private).{0,20}?[\"'=:\s]\s*[\"']?([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])")),
    ("github-token", re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("stripe-secret-key", re.compile(r"\b((?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,})\b")),
    ("slack-token", re.compile(r"\b(xox[abposr]-[A-Za-z0-9-]{10,})")),
    ("anthropic-api-key", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{20,})")),
    ("openai-api-key", re.compile(r"\b(sk-(?:proj-)?[A-Za-z0-9]{32,})\b")),
    ("google-api-key", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b")),
    ("npm-token", re.compile(r"\b(npm_[A-Za-z0-9]{36})\b")),
    ("private-key", re.compile(r"(-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----)")),
    ("jwt", re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")),
    # KEY="value" in code or config, quoted, with a secret-sounding key.
    ("generic-secret", re.compile(
        r"(?i)\b[\w.-]*(?:password|passwd|secret|api_?key|access_?token|auth_?token|client_?secret|private_?key)"
        r"[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']")),
    # KEY=value env style (unquoted), as in a .env file or a pasted shell export; not code like
    # `const API_TOKEN = getToken()`.
    ("generic-secret", re.compile(
        r"(?<![\w$.])[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*=\s*"
        r"([^\s#\"'$(),;{}\[\]]{8,})(?=\s|$)")),
]

PLACEHOLDER = re.compile(
    r"(?i)^(?:x+|\*+|\.+|<.*>|\$\{?.*|%.*%|\{\{.*\}\}|changeme.*|example.*|your[_-].*|dummy.*|placeholder.*"
    r"|redacted.*|«redacted.*|process\.env.*|import\.meta.*|test(?:ing)?(?:password|secret)?\d*|password\d*)$")

# Names of .env keys whose values are secret. Everything else in .env (URLs, client ids, flags)
# is configuration that legitimately shows up in specs, logs and the public bundle.
SECRET_KEY_NAME = re.compile(r"(?i)(PASSWORD|PASSWD|_PASS$|SECRET|TOKEN|PRIVATE|CREDENTIAL|API_?KEY|ACCESS_?KEY|AUTH_?KEY|DSN|SIGNING|SALT)")

# ---- secret files ------------------------------------------------------------------------------------
ENV_SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")
SECRET_FILE_GLOBS = (
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_ecdsa*", "id_ed25519*", "id_dsa*",
    ".npmrc", ".pypirc", ".netrc", ".pgpass", ".git-credentials", ".htpasswd", "credentials.json",
    "service-account*.json", "*.tfvars", "terraform.tfstate*",
)
SECRET_DIR_PARTS = (("/.aws/",), ("/.ssh/",), ("/.gnupg/",), ("/.config/gh/",), ("/.docker/config.json",),
                    ("/.kube/config",), ("/.azure/",), ("/.config/gcloud/",))


def is_env_file(name: str) -> bool:
    """`.env`, `.env.local`, `prod.env`... but not `.env.example` and friends."""
    if name.endswith(ENV_SAFE_SUFFIXES):
        return False
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def is_secret_path(path: str) -> bool:
    if not path:
        return False
    p = path.replace("\\", "/")
    name = p.rstrip("/").rsplit("/", 1)[-1]
    if is_env_file(name):
        return True
    if any(fnmatch.fnmatch(name, g) for g in SECRET_FILE_GLOBS):
        return True
    expanded = os.path.expanduser(p) if p.startswith("~") else p
    probe = "/" + expanded.lstrip("/")
    return any(part in probe or probe.endswith(part.rstrip("/")) for (part,) in SECRET_DIR_PARTS) or \
        ("/proc/" in probe and probe.endswith("/environ"))


# ---- .env values -------------------------------------------------------------------------------------
def project_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        out[m.group(1)] = value
    return out


def env_secrets(root: Path | None = None) -> list[tuple[str, str]]:
    """(KEY, value) for secret-named keys in the repo's .env files. Values stay in memory only."""
    root = root or project_dir()
    found: dict[str, str] = {}
    for f in sorted(root.glob(".env*")):
        if not f.is_file() or not is_env_file(f.name):
            continue
        try:
            pairs = _parse_env(f.read_text(errors="replace"))
        except OSError:
            continue
        for key, value in pairs.items():
            if not SECRET_KEY_NAME.search(key) or len(value) < 8:
                continue
            if re.match(r"(?i)^(true|false|null|none|\d+|https?://\S*)$", value) or PLACEHOLDER.match(value):
                continue
            found[key] = value
    return sorted(found.items())


# ---- matching ------------------------------------------------------------------------------------
def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def load_allow(path: Path = HERE / "allow.txt") -> set[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    return {ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()}


def find_secrets(text: str, env: list[tuple[str, str]] | None = None,
                 allow: set[str] | None = None) -> list[dict]:
    """[{rule, start, end, fingerprint}] for every secret in text, minus allow-listed fingerprints."""
    allow = allow if allow is not None else load_allow()
    hits: list[dict] = []
    for rule, rx in SECRET_PATTERNS:
        for m in rx.finditer(text):
            group = m.lastindex or 0
            secret = m.group(group)
            if rule == "generic-secret" and PLACEHOLDER.match(secret):
                continue
            hits.append({"rule": rule, "start": m.start(group), "end": m.end(group), "fingerprint": fingerprint(secret)})
    for key, value in env or []:
        start = text.find(value)
        while start != -1:
            hits.append({"rule": f"env-value:{key}", "start": start, "end": start + len(value),
                         "fingerprint": fingerprint(value)})
            start = text.find(value, start + 1)
    hits = [h for h in hits if h["fingerprint"] not in allow]
    # One hit per span: keep the earliest-starting, longest match.
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    out: list[dict] = []
    for h in hits:
        if out and h["start"] < out[-1]["end"]:
            continue
        out.append(h)
    return out


def redact(text: str, env: list[tuple[str, str]] | None = None) -> str:
    for h in reversed(find_secrets(text, env, allow=set())):
        text = text[: h["start"]] + f"«redacted:{h['rule']}»" + text[h["end"]:]
    return text


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ---- scanning ------------------------------------------------------------------------------------
def scan_git(worktree: str, merge_base: str, env, allow) -> list[dict]:
    """Every line added by any commit in merge_base..HEAD, so a secret added then removed still counts."""
    log = subprocess.run(
        ["git", "-C", worktree, "log", "-p", "-U0", "--no-color", "--no-ext-diff", "--format=commit %H",
         f"{merge_base}..HEAD"],
        capture_output=True, text=True, errors="replace", check=True).stdout
    findings, commit, path, line = [], "", "", 0
    for raw in log.splitlines():
        if raw.startswith("commit "):
            commit = raw.split()[1]
        elif raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            for h in find_secrets(raw[1:], env, allow):
                findings.append({"file": path, "line": line, "commit": commit[:10], "rule": h["rule"],
                                 "fingerprint": h["fingerprint"]})
            line += 1
    return findings


def scan_file(path: str, env, allow) -> list[dict]:
    p = Path(path)
    if not p.is_file() or p.stat().st_size > 5_000_000:
        return []
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        return []
    text = data.decode(errors="replace")
    return [{"file": str(p), "line": line_of(text, h["start"]), "rule": h["rule"], "fingerprint": h["fingerprint"]}
            for h in find_secrets(text, env, allow)]


def filter_gitleaks(report: str, allow) -> list[dict]:
    """Turn a gitleaks JSON report into our findings shape, dropping the Secret/Match values."""
    try:
        items = json.loads(Path(report).read_text() or "[]")
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for it in items or []:
        fp = fingerprint(it.get("Secret", ""))
        if fp in allow:
            continue
        out.append({"file": it.get("File", ""), "line": it.get("StartLine", 0), "commit": (it.get("Commit") or "")[:10],
                    "rule": f"gitleaks:{it.get('RuleID', '?')}", "fingerprint": fp})
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="policy.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--out", required=True)
    s.add_argument("--git", nargs=2, metavar=("WORKTREE", "MERGE_BASE"))
    s.add_argument("--file", action="append", default=[])
    s.add_argument("--gitleaks-report", action="append", default=[])
    s.add_argument("--tool", default="builtin")
    sub.add_parser("redact")
    c = sub.add_parser("check-env")
    c.add_argument("--require", default="")
    c.add_argument("--match", default="")
    c.add_argument("--pattern", default="")
    args = ap.parse_args(argv)

    if args.cmd == "check-env":
        f = project_dir() / ".env"
        if not f.is_file():
            print(f"check-env: {f} missing", file=sys.stderr)
            return 1
        values = _parse_env(f.read_text(errors="replace"))
        bad = [f"{k} missing or empty" for k in filter(None, args.require.split(",")) if not values.get(k)]
        for k in filter(None, args.match.split(",")):
            if not values.get(k):
                bad.append(f"{k} missing or empty")
            elif not re.search(args.pattern, values[k]):
                bad.append(f"{k} does not match /{args.pattern}/")
        for b in bad:
            print(f"check-env: {b}", file=sys.stderr)
        print("check-env: ok" if not bad else "check-env: FAILED")
        return 1 if bad else 0

    env = env_secrets()
    if args.cmd == "redact":
        for chunk in sys.stdin:
            sys.stdout.write(redact(chunk, env))
            sys.stdout.flush()
        return 0

    allow = load_allow()
    findings: list[dict] = []
    for report in args.gitleaks_report:
        findings += filter_gitleaks(report, allow)
    if args.git:
        findings += scan_git(args.git[0], args.git[1], env, allow)
    for f in args.file:
        findings += scan_file(f, env, allow)
    seen, unique = set(), []
    for f in findings:  # gitleaks and the built-in rules often report the same secret
        key = (os.path.basename(f["file"]), f["line"], f["fingerprint"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    findings = unique
    Path(args.out).write_text(json.dumps({"tool": args.tool, "findings": findings}, indent=2) + "\n")
    for f in findings:
        where = f"{f['file']}:{f['line']}" + (f" ({f['commit']})" if f.get("commit") else "")
        print(f"secret-scan: {where} {f['rule']} fingerprint={f['fingerprint']}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
